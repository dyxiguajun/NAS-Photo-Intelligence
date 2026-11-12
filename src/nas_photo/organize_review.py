"""Apply an explicit browse decision to the exact reviewed pilot cohort."""
from collections import Counter, defaultdict

from .config import relative
from .organize_index import SHA256


EXCLUDED_BY_USER = 'excluded_from_gallery_by_user'
REVIEWED_CATEGORIES = ('carved_recovery', 'small_recovery', 'preview_or_metadata_warning')


def reviewed_pilot_browse(files, source_map, gallery):
    """Reconcile the reviewed view with the sealed pilot's L0 groups."""
    by_path = {}
    for row in files:
        path = row['destination']
        if (path in by_path or relative(path) != path or not SHA256.fullmatch(row['sha256'])
                or type(row['size']) is not int or row['size'] < 0):
            raise ValueError('Invalid or repeated materialized pilot identity')
        by_path[path] = row
    grouped = defaultdict(list)
    source_ids = set()
    for row in source_map:
        source = tuple(row['source'])
        path = row.get('retained_at')
        file = by_path.get(path)
        if (source in source_ids or file is None or row['ledger_state'] not in ('processed','excluded')
                or row['execution_allowed'] is not False
                or (row['sha256'],row['size']) != (file['sha256'],file['size'])):
            raise ValueError('Pilot L0 source map does not match the verified copy')
        source_ids.add(source)
        grouped[path].append(row)
    dispositions = {}
    intersections = Counter()
    totals = Counter()
    for item in gallery:
        path = item['path']
        file = by_path.get(path)
        if (file is None or path in dispositions or (item['sha256'],item['bytes']) != (file['sha256'],file['size'])):
            raise ValueError('Gallery identity differs from the frozen pilot copy')
        origins = grouped[path]
        identities = [tuple(origin['source']) for origin in origins]
        listed = [(origin['sid'],origin['path']) for origin in item['sources']]
        kept = [origin['source'] for origin in origins if origin['ledger_state']=='processed']
        if sorted(listed)!=sorted(identities) or kept != [file['copied_from']]:
            raise ValueError('Gallery source mapping differs from the sealed L0 group')
        carved = any(origin['source'][0]=='recovered' for origin in origins)
        recovery_source = carved or any(origin['source'][0]=='restored' for origin in origins)
        width, height = item['width'], item['height']
        small = bool(recovery_source and width and height and min(width,height)<600)
        if item['small_recovery'] is not small:
            raise ValueError('Gallery size flag differs from the reviewed dimensions and sources')
        warning = bool(item.get('preview_error') or item.get('format_mismatch') or item.get('warnings'))
        reasons = [reason for reason, applies in zip(REVIEWED_CATEGORIES,(carved,small,warning)) if applies]
        intersections['|'.join(reasons) or 'none'] += 1
        totals.update(reasons)
        evidence = []
        for origin in origins:
            record = {'source':origin['source'],'stat':origin.get('stat'),'copy_role':origin['ledger_state']}
            if recovery := origin.get('recovery'):
                record['recovery'] = {key:recovery[key] for key in
                                      ('frozen_parent','container_sha256','offset','length','object_sha256')}
            evidence.append(record)
        dispositions[path] = {**file, 'status':EXCLUDED_BY_USER if reasons else 'visible_in_reviewed_pilot',
                              'reasons':reasons,'sources':evidence,'execution_allowed':False}
    if dispositions.keys()!=by_path.keys():
        raise ValueError('Gallery review does not cover the full frozen pilot')
    omitted = [row for row in dispositions.values() if row['status']==EXCLUDED_BY_USER]
    removed = {row['destination'] for row in omitted}
    visible = [row for row in source_map if row['retained_at'] not in removed]
    return list(dispositions.values()), visible, {
        'denominator':len(by_path),'reviewed_category_counts':dict(totals),'exact_intersections':dict(intersections),
        'excluded_media':len(omitted),'visible_media':len(by_path)-len(omitted),
        'excluded_source_rows':len(source_map)-len(visible),'visible_source_rows':len(visible),
        'excluded_logical_bytes':sum(row['size'] for row in omitted),'execution_allowed':False,
    }
