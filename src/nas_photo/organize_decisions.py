"""Deterministic recommendations with explicit evidence and review boundaries."""
from collections import defaultdict
from datetime import datetime
from hashlib import sha256
from math import isfinite
from pathlib import PurePosixPath
import re
import unicodedata


def identity(row):
    return [row['source_id'], row['relative_path']]


def l0_key(row):
    digest = row['evidence'].get('sha256')
    return (row['size'], digest) if digest else None


def l1_key(row):
    visual = row['evidence'].get('visual', {})
    pixel = visual.get('pixel_sha256')
    pipeline = visual.get('pipeline_key')
    return (pipeline, pixel) if visual.get('status') == 'valid' and pipeline and pixel else None


def assessed_quality(evidence):
    detail = evidence.get('quality', {})
    measured, damage = detail.get('detail_score'), detail.get('compression_damage')
    if (detail.get('assessment_id') and type(measured) in (int,float) and type(damage) in (int,float)
            and isfinite(measured) and isfinite(damage)):
        return measured, damage
    return None


def quality(row):
    e = row['evidence']
    visual = e.get('visual', {})
    integrity = {'valid':2, 'failed':0, 'unsupported':1, 'limited':1}.get(visual.get('status'), 1)
    detail = e.get('quality', {})
    derivative = detail.get('derivation')
    provenance = {'original':2, 'thumbnail':0, 'upscaled':0, 'recompressed':0}.get(derivative, 1)
    width, height = visual.get('width') or e.get('width'), visual.get('height') or e.get('height')
    pixels = width * height if width and height else 0
    if derivative == 'upscaled':
        pixels = 0
    measured, damage = assessed_quality(e) or (0,0)
    metadata = e.get('metadata', {})
    completeness = sum(field_value(field,metadata.get(field)) is not None
                       for field in ('date_time_original','gps','make','model','lens_model'))
    return integrity, provenance, measured, -damage, pixels, completeness


def version_rank(row):
    return tuple(-value for value in quality(row)), identity(row)


def exact_copy_keeper(members):
    return min(members, key=lambda row: (not bool(row['evidence'].get('recovery_container')), version_rank(row)))


def capture(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value[:10].replace(':','-')+value[10:])
    except ValueError:
        return None
    if parsed.replace(tzinfo=None) == datetime(1970,1,1) or len(value) < 19:
        return None
    return parsed.isoformat(sep=' ')


def gps(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lat, lon = value
    if not all(type(n) in (int, float) and isfinite(n) for n in (lat, lon)):
        return None
    return [lat, lon] if abs(lat) <= 90 and abs(lon) <= 180 else None


def field_value(field, value):
    if field == 'date_time_original':
        return capture(value)
    if field == 'gps':
        return gps(value)
    return value.strip() if isinstance(value, str) and value.strip() else None


def observed_field(metadata, field):
    raw = metadata.get(field)
    if field == 'date_time_original':
        offset = metadata.get('offset_time_original')
        if isinstance(raw, str) and isinstance(offset, str) and re.fullmatch(r'[+-]\d\d:\d\d', offset):
            try:
                parsed = datetime.fromisoformat(raw[:10].replace(':','-')+raw[10:])
                if parsed.tzinfo is None:
                    return capture(raw + offset)
            except ValueError:
                pass
    return field_value(field, raw)


def equal_field(field, left, right):
    if field != 'date_time_original':
        return left == right
    a, b = datetime.fromisoformat(left), datetime.fromisoformat(right)
    if a.tzinfo is None or b.tzinfo is None:
        return left == right
    return a == b


def proposals(target, members, relation):
    recommendations = []
    for field in ('date_time_original', 'gps', 'make', 'model', 'lens_model'):
        target_meta = target['evidence'].get('metadata', {})
        raw = target_meta.get(field)
        current = observed_field(target_meta, field)
        observations = []
        for row in members:
            if row is target:
                continue
            meta = row['evidence'].get('metadata', {})
            value = observed_field(meta, field)
            if value is not None:
                trusted = (field in row['evidence'].get('trusted_fields', []) and
                           row['evidence'].get('visual', {}).get('status') == 'valid')
                observations.append({'source':identity(row), 'source_stat':[row.get(k) for k in ('size','mtime_ns','ctime_ns')],
                                     'source_sha256':row['evidence'].get('sha256'),
                                     'value':value, 'trusted':trusted,
                                     'field_provenance':row['evidence'].get('field_provenance', {}).get(field)})
        if not observations:
            continue
        preferred = observations[0]['value']
        conflict = any(not equal_field(field, item['value'], preferred) for item in observations) or (
            current is not None and not equal_field(field, current, preferred))
        trusted = [item for item in observations if item['trusted']]
        if not conflict and (current is not None or not trusted):
            continue
        recommendations.append({'field':field,'target':identity(target),
                                'target_stat':[target.get(k) for k in ('size','mtime_ns','ctime_ns')],
                                'target_sha256':target['evidence'].get('sha256'),
                                'source':trusted[0]['source'] if trusted and not conflict else None,
                                'original':raw,'proposed':preferred if trusted and not conflict else None,
                                'conflict':conflict,'observations':observations,'relation':relation,
                                'confidence':'conflicted' if conflict else 'trusted_field_with_direct_relation',
                                'requires_review':True,'write_authorized':False})
    return recommendations


def l1_version_review(row, primary):
    evidence, primary_evidence = row['evidence'], primary['evidence']
    visual = evidence.get('visual', {})
    metadata, primary_metadata = evidence.get('metadata', {}), primary_evidence.get('metadata', {})
    fields = sorted(field for field in metadata.keys() | primary_metadata.keys()
                    if metadata.get(field) != primary_metadata.get(field))
    _, _, _, _, pixels, completeness = quality(row)
    detail, damage = assessed_quality(evidence) or (None,None)
    hold = 'preferred_version' if row is primary else (
        'keep_visible_metadata_differs' if fields else 'review_as_secondary_version')
    return {'encoded_format':visual.get('format'), 'encoded_dimensions':visual.get('source_dimensions'),
            'normalized_dimensions':[visual.get('width'),visual.get('height')],
            'effective_pixels':pixels, 'detail_score':detail, 'compression_damage':damage,
            'recognized_metadata_fields':completeness, 'different_metadata_fields':fields,
            'display_advice':hold,'external_associations':'unverified',
            'visible':True,'hide_requires_review':True,'execution_allowed':False}


def recommendation(members, relation, reviewed_pairs=()):
    if len(members) < 2 or any(row['state'] != 'present' for row in members):
        raise ValueError('Version selection requires at least two current files')
    if len({tuple(identity(row)) for row in members}) != len(members):
        raise ValueError('Duplicate member')
    ranked = sorted(members, key=version_rank)
    target = ranked[0]
    review_ids = []
    if relation == 'reviewed_same_capture':
        def revision(row):
            return (*identity(row), row['size'], row['mtime_ns'], row['ctime_ns'])
        edges = {frozenset((tuple(pair['a']), tuple(pair['b']))):pair['review_id'] for pair in reviewed_pairs
                 if pair.get('status') == 'confirmed_same_capture' and pair.get('review_id')}
        required = [frozenset((revision(target), revision(row))) for row in ranked[1:]]
        if any(edge not in edges for edge in required):
            raise ValueError('Each version requires a direct reviewed relation to the proposed representative')
        review_ids = sorted({edges[edge] for edge in required})
    else:
        key = {'L0':l0_key, 'L1':l1_key}.get(relation)
        if key is None or None in {key(row) for row in members} or len({key(row) for row in members}) != 1:
            raise ValueError('A shared verified L0 or L1 equality key is required')
    integrity, derivation, measured, damage, pixels, completeness = quality(target)
    reasons = ['full_decode_verified' if integrity == 2 else 'full_decode_not_verified',
               {'derivation':'original_evidence' if derivation == 2 else 'provenance_or_derivative_review'},
               {'effective_pixels':pixels, 'assessed_detail_score':measured,
                'assessed_compression_damage':-damage, 'recognized_metadata_fields':completeness}]
    # Binary duplicates contain the same embedded metadata. External associations stay at each path.
    versions = [{'source':identity(row), 'version':row['version'],
                 'stat':[row.get(k) for k in ('size','mtime_ns','ctime_ns')],
                 'sha256':row['evidence'].get('sha256'),
                 'decode':row['evidence'].get('visual',{}).get('status','unverified'),
                 'advice':('proposed_keep' if row is target and integrity==2 else
                           'representative_needs_decode_review' if row is target else 'preserve_pending_review')}
                for row in ranked]
    if relation == 'L1':
        for version, row in zip(versions, ranked):
            version['l1_review'] = l1_version_review(row, target)
    return {'preferred':identity(target) if integrity == 2 else None,
            'review_representative':identity(target),'reasons':reasons,'relation':relation,
            'alternatives':[identity(row) for row in ranked[1:]],
            'versions':versions,'review_ids':review_ids,
            'metadata':proposals(target, ranked, relation) if relation != 'L0' and integrity == 2 else [],
            'requires_review':True, 'execution_allowed':False}


def portable_key(path):
    return unicodedata.normalize('NFC', path).casefold()


def destinations(rows):
    """Keep ordinary names and reserve deterministic alternatives for portable conflicts."""
    names = {tuple(identity(row)):str(PurePosixPath(*identity(row))) for row in rows}
    counts = defaultdict(int)
    directories = set()
    for path in names.values():
        key = portable_key(path)
        counts[key] += 1
        directories.update(str(parent) for parent in PurePosixPath(key).parents)
    reserved = set(counts)
    result = {}
    for identity_key, original in sorted(names.items()):
        key = portable_key(original)
        if counts[key] == 1 and key not in directories:
            result[identity_key] = original
            continue
        path = PurePosixPath(original)
        digest = sha256('\0'.join(identity_key).encode()).hexdigest()[:16]
        for attempt in range(len(names)+1):
            suffix = f'--{digest}' if not attempt else f'--{digest}-{attempt}'
            ext = path.suffix.encode()[:32].decode('utf-8','ignore')
            room = 255-len((suffix+ext).encode())
            stem = path.stem.encode()[:room].decode('utf-8','ignore')
            alternative = str(path.with_name(stem+suffix+ext))
            candidate = portable_key(alternative)
            if candidate not in reserved and candidate not in directories:
                reserved.add(candidate)
                result[identity_key] = alternative
                break
        else:
            raise ValueError(f'Could not allocate a portable destination: {original}')
    return names, result


def simulation(rows, reviewed_groups=()):
    present = [row for row in rows if row['state'] == 'present']
    exact, pixels = defaultdict(list), defaultdict(list)
    for row in present:
        if key := l0_key(row):
            exact[key].append(row)
        if key := l1_key(row):
            pixels[key].append(row)
    exact_decisions, copy_targets, pixel_decisions, pixel_reviews, metadata = {}, {}, {}, {}, []
    originals, assigned = destinations(present)
    for groups, label, output in ((exact,'L0',exact_decisions),(pixels,'L1',pixel_decisions)):
        for members in groups.values():
            if len(members) < 2 or (label == 'L1' and len({l0_key(row) for row in members}) == 1
                                     and l0_key(members[0]) is not None):
                continue
            rec = recommendation(members, label)
            metadata.extend(rec['metadata'])
            for row in members:
                output[tuple(identity(row))] = rec
            if label == 'L0':
                keeper = identity(exact_copy_keeper(members))
                for row in members:
                    key = tuple(identity(row))
                    copy_targets[key] = identity(row) if row['evidence'].get('recovery_container') else keeper
            else:
                pixel_reviews.update((tuple(version['source']),version['l1_review']) for version in rec['versions'])
    manually_reviewed = {}
    by_identity = {tuple(identity(row)):row for row in rows}
    for group in reviewed_groups:
        members = [by_identity[tuple(key)] for key in group['members']]
        decision = recommendation(members, 'reviewed_same_capture', group.get('reviewed_pairs', ()))
        for row in members:
            key = tuple(identity(row))
            if key in manually_reviewed:
                raise ValueError('Reviewed groups must not overlap')
            manually_reviewed[key] = decision
        metadata.extend(decision['metadata'])
    items = []
    for row in rows:
        key = tuple(identity(row))
        original = originals.get(key)
        target = assigned.get(key)
        exact_group, pixel_group = exact_decisions.get(key), pixel_decisions.get(key)
        action = 'retain_pending_search' if row['state'] == 'present' else 'source_unavailable_review'
        if exact_group and list(key) != exact_group['review_representative']:
            action = 'l0_redundant_review'
        elif pixel_group and not exact_group:
            action = 'l1_equal_pixels_review'
        elif exact_group:
            action = 'l0_representative_review'
        elif key in manually_reviewed:
            action = 'reviewed_versions_pending_plan_approval'
        evidence = row['evidence']
        protection = evidence.get('recovery_container')
        if protection:
            action = 'retain_recovery_review'
        decode = evidence.get('visual', {}).get('status')
        item = {'source':list(key), 'observed_version':row['version'], 'size':row['size'],
                'state':row['state'],'action':action,'proposed_destination':target,
                'requested_destination':original,'destination_renamed':target != original,
                'source_stat':[row.get(k) for k in ('size','mtime_ns','ctime_ns')],
                'decode_status':decode or 'unverified',
                'metadata_issue':evidence.get('metadata_error'),
                'capture_time_unresolved':capture(evidence.get('metadata',{}).get('date_time_original')) is None,
                'sha256':evidence.get('sha256'),'execution_allowed':False}
        if protection:
            item['recovery_protection'] = protection
        if exact_group:
            item['l0_review_representative'] = exact_group['review_representative']
            item['l0_preferred'] = exact_group['preferred']
            item['l0_copy_target'] = copy_targets[key]
        if pixel_group:
            item['l1_review_representative'] = pixel_group['review_representative']
            representative = by_identity[tuple(pixel_group['review_representative'])]
            item['l1_metadata_differs_from_representative'] = (
                row['evidence'].get('metadata') != representative['evidence'].get('metadata'))
            item['l1_version_review'] = pixel_reviews[key]
        if key in manually_reviewed:
            item['reviewed_same_capture_representative'] = manually_reviewed[key]['review_representative']
            item['review_ids'] = manually_reviewed[key]['review_ids']
        items.append(item)
    known_bytes = sum(row['size'] for row in present)
    redundant = sum((len(group)-1)*group[0]['size'] for group in exact.values())
    return items, metadata, {
        'registered':len(rows),'present':len(present),'unavailable_or_missing':len(rows)-len(present),
        'present_logical_bytes_keep_all':known_bytes,'l0_logical_redundancy':redundant,
        'present_logical_bytes_after_l0_model':known_bytes-redundant,
        'actual_physical_bytes_saved':None,'authorized_cleanup_count':0,
        'destination_renames':sum(item['destination_renamed'] for item in items),
        'l0_groups':sum(len(group)>1 for group in exact.values()),
        'l1_review_members':len(pixel_decisions),'reviewed_same_capture_members':len(manually_reviewed),
        'metadata_proposals':len(metadata),
    }
