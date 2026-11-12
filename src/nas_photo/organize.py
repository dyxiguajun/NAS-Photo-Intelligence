"""Read-only planning CLI over the local audit database."""
from collections import Counter
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os

from . import organize_index as index
from . import organize_evidence as evidence
from .organize_decisions import identity, l0_key, l1_key, recommendation, simulation
from .organize_exercise import checksum
from .similarity import comparison_options, pair_evidence


def configure(parser):
    parser.add_argument('--database', default='reports/datarestore-audit/analysis.sqlite3')
    sub = parser.add_subparsers(dest='organize_command', required=True)
    sub.add_parser('status')
    sub.add_parser('register')
    manifest = sub.add_parser('manifest')
    manifest.add_argument('--source', required=True)
    manifest.add_argument('--require-complete-sha', action='store_true')
    ingest = sub.add_parser('ingest')
    ingest.add_argument('--manifest', required=True)
    for command in ('verify-evidence','ingest-evidence'):
        sub.add_parser(command).add_argument('--bundle', required=True)
    plan = sub.add_parser('simulate')
    plan.add_argument('--output', required=True)
    plan.add_argument('--reviewed-groups')
    plan.add_argument('--candidate-queries')
    plan.add_argument('--frozen-manifest', type=Path)
    plan.add_argument('--frozen-sha256')
    review = sub.add_parser('recommend')
    review.add_argument('--group', required=True)
    lookup = sub.add_parser('lookup')
    lookup.add_argument('--source', required=True)
    lookup.add_argument('--path', required=True)
    lookup.add_argument('--reference-source', action='append', required=True)
    lookup.add_argument('--top-k', type=int, default=10)
    compare = sub.add_parser('reconcile')
    compare.add_argument('--source-manifest', required=True)
    compare.add_argument('--destination-manifest', required=True)
    compare.add_argument('--source-sha256')
    compare.add_argument('--destination-sha256')
    match_sha = sub.add_parser('match-sha')
    match_sha.add_argument('--frozen-manifest',required=True,type=Path)
    match_sha.add_argument('--manifest-sha256',required=True)
    match_sha.add_argument('--objects',required=True,type=Path)
    preflight = sub.add_parser('prepare-build')
    preflight.add_argument('--plan', required=True)
    preflight.add_argument('--output', required=True)
    for parser in (plan, preflight, compare):
        parser.add_argument('--release-tag', help='Use a clean, reviewed release for formal preflight')
        parser.add_argument('--config-sha256', help='Required config digest for formal preflight')
    fixtures = sub.add_parser('exercise').add_mutually_exclusive_group()
    fixtures.add_argument('--image-fixtures',action='store_true')
    fixtures.add_argument('--build-fixtures',action='store_true')
    fixtures.add_argument('--rclone-fixtures', type=Path, metavar='RCLONE')


def audit_path(project, path):
    target = Path(path).resolve()
    reports = project / 'reports'
    dsh = reports / 'datarestore-audit/dsh'
    formal = project / 'data/index.sqlite3'
    if (target == reports or not target.is_relative_to(reports) or target.is_relative_to(dsh)
            or (target.exists() and formal.exists() and target.samefile(formal))):
        raise ValueError('Organization outputs must stay in the local reports workspace outside dsh/')
    return target


def output_plan(db, output, reviewed_groups=(), candidate_queries=(), *,
                provenance=None, inputs=None):
    output = Path(output)
    if output.exists():
        raise ValueError('Plan output already exists; use a new directory')
    rows = index.files(db)
    formal = provenance is not None and 'release_tag' in provenance
    if formal:
        from .organize_build import require_complete_sha
        from .organize_sha import frozen_revisions
        require_complete_sha(rows)
        if not inputs or 'frozen-inventory.input.jsonl' not in inputs:
            raise ValueError('Formal simulation requires a pinned frozen inventory')
        frozen = frozen_revisions(inputs['frozen-inventory.input.jsonl'], provenance.get('frozen_sha256'))
        current = {tuple(identity(row)):tuple(row[k] for k in ('size','mtime_ns','ctime_ns')) for row in rows}
        if frozen != current:
            raise ValueError('Current source identities differ from the frozen inventory')
    edges, outcomes, stale = evidence.current(db, rows)
    counts = Counter(tuple(edge[side][:2]) for edge in edges
                     if edge['relation'] in ('confirmed_same_capture','ambiguous','conflict','low_information')
                     for side in ('a','b'))
    occupied = {tuple(key) for group in reviewed_groups for key in group['members']}
    groups = [*reviewed_groups]
    for edge in edges:
        a,b = (tuple(edge[side][:2]) for side in ('a','b'))
        if edge['relation']=='confirmed_same_capture' and counts[a]==counts[b]==1 and not ({a,b}&occupied):
            groups.append({'members':[a,b], 'reviewed_pairs':[{**edge,'status':'confirmed_same_capture'}]})
    items, proposals, summary = simulation(rows, groups)
    by_id = {tuple(row['source']):row for row in items}
    unresolved = {tuple(edge[side][:2]) for edge in edges if edge['relation'] in
                  ('visual_candidate','ambiguous','conflict','low_information') for side in ('a','b')}
    if len(candidate_queries) > 100:
        raise ValueError('Simulation accepts at most 100 explicit cached visual queries')
    query_results = [lookup(rows, request['query'], set(request['reference_sources']),
                            request.get('top_k', 10)) for request in candidate_queries]
    visual = [dict(candidate, query=result['query'], query_version=result['query_version'],
                   reference_sources=result['reference_sources'])
              for result in query_results for candidate in result['candidates']
              if candidate['relation']=='L2_candidate']
    summary['actions'] = dict(Counter(row['action'] for row in items))
    overview = index.status(db, rows)
    summary['source_coverage'] = overview['sources']
    summary['cross_source_l0_groups'] = overview['cross_source_l0_groups']
    summary['visual_candidate_coverage'] = 'explicit_cached_queries_only'
    summary['cached_queries'] = len(query_results)
    summary['visual_candidates'] = len(visual)
    summary['validated_evidence_edges'] = len(edges)
    summary['stale_evidence_runs'] = stale
    summary['capacity_model'] = 'logical_bytes_for_present_indexed_paths_only'
    summary.update(mode='formal_preflight' if formal else 'exploratory',
                   execution_allowed=False, code_provenance=provenance)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.organize-plan-', dir=output.parent) as staging:
        root = Path(staging) / 'plan'
        root.mkdir()
        def cleanup_rows():
            for row in items:
                key = tuple(row['source'])
                if row['action']!='l0_redundant_review' or not row.get('l0_preferred') or key in unresolved:
                    continue
                target = by_id[tuple(row['l0_preferred'])]
                yield dict(row, replacement={k:target[k] for k in ('source','source_stat','sha256','proposed_destination')},
                           confirmation={'relation':'L0','sha256':row['sha256']},
                           metadata_check={'embedded':'equal_bytes','external':'unverified'},
                           companions='unverified', restore_copy='unverified', final_location='unverified',
                           recovery_prerequisites=['replacement_sha_verified_at_final_location',
                               'independent_recovery_copy_verified','restore_test_passed','explicit_cleanup_approval'])

        exports = {
            'inventory-disposition.jsonl':items,
            'retain-or-review.jsonl':(r for r in items if r['state'] == 'present'),
            'l0-redundant-review.jsonl':(r for r in items if r.get('l0_review_representative') != r['source']
                                        and 'l0_review_representative' in r),
            'l1-equal-pixels-review.jsonl':(r for r in items if 'l1_review_representative' in r),
            'no-confirmed-alternative.jsonl':(r for r in items if r['action'] == 'retain_pending_search'),
            'confirmed-retain.jsonl':(r for r in items if r['state']=='present' and (
                                      r.get('recovery_protection') or not r.get('l0_preferred')
                                      or r['l0_preferred']==r['source'])),
            'recovery-source-protection.jsonl':(r for r in items if r.get('recovery_protection')),
            'manual-review.jsonl':(r for r in items if r['action'] != 'retain_pending_search'
                                   or tuple(r['source']) in unresolved or r['decode_status']=='failed'),
            'damage-or-unavailable.jsonl':(r for r in items if r['decode_status']=='failed'
                                          or r['metadata_issue'] or r['state']!='present'),
            'destination-renames.jsonl':(r for r in items if r['destination_renamed']),
            'future-cleanup-review.jsonl':cleanup_rows(),
            'metadata-proposals.jsonl':proposals,
            'visual-candidates.jsonl':visual,
            'query-coverage.jsonl':query_results,
            'review-evidence.jsonl':edges,
            'review-outcomes.jsonl':outcomes,
        }
        for name, records in exports.items():
            with (root / name).open('w', encoding='utf-8') as stream:
                for row in records:
                    stream.write(index.encode(row)+'\n')
                stream.flush()
                os.fsync(stream.fileno())
        summary['disposition_sha256'] = checksum(root/'inventory-disposition.jsonl')
        if inputs:
            summary['input_sha256'] = {}
            for name, content in inputs.items():
                with os.fdopen(os.open(root/name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                summary['input_sha256'][name] = sha256(content).hexdigest()
        (root / 'summary.json').write_text(index.encode(summary)+'\n')
        if output.exists():
            raise ValueError('Plan output appeared while generating')
        root.rename(output)
    return {'output':str(output), **summary}


def lookup(rows, query_id, reference_sources, k):
    if not 1 <= k <= 100:
        raise ValueError('Top-K must be 1..100')
    query = next((row for row in rows if identity(row) == query_id), None)
    if not query or query['state'] != 'present':
        raise ValueError('Query is absent from the current index')
    qvis = query['evidence'].get('visual', {})
    groups, scanned, compatible = {}, 0, 0
    coverage = Counter()
    params = comparison_options()
    for row in rows:
        if row['source_id'] not in reference_sources or row['state'] != 'present' or row is query:
            continue
        scanned += 1
        coverage[(row['source_id'],row['version'])] += 1
        rvis = row['evidence'].get('visual', {})
        evidence = None
        same_profile = (qvis.get('status') == rvis.get('status') == 'valid' and
                        qvis.get('pipeline_key') and qvis['pipeline_key'] == rvis.get('pipeline_key'))
        if same_profile:
            compatible += 1
        if l0_key(query) and l0_key(query) == l0_key(row):
            relation, rank = 'L0', (0,0,0)
        elif l1_key(query) and l1_key(query) == l1_key(row):
            relation, rank = 'L1', (1,0,0)
        elif same_profile and qvis.get('result') and rvis.get('result'):
            evidence, _ = pair_evidence(qvis['result'], rvis['result'], params)
            if not evidence:
                continue
            relation, rank = 'L2_candidate', (2,evidence['phash_hamming'],evidence['dhash_hamming'])
        else:
            continue
        ref = identity(row)
        key = l0_key(row) or tuple(ref)
        item = (rank, ref, {'reference':ref,'reference_version':row['version'],'relation':relation,
                            'query_dimensions':[qvis.get(key) or query['evidence'].get(key) for key in ('width','height')],
                            'reference_dimensions':[rvis.get(key) or row['evidence'].get(key) for key in ('width','height')],
                            'evidence':evidence,'requires_review':True})
        if key not in groups or item[:2] < groups[key][:2]:
            groups[key] = item
    candidates = [row[2] for row in sorted(groups.values())[:k]]
    return {'query':query_id,'query_version':query['version'],'reference_sources':sorted(reference_sources),
            'present_reference_paths':scanned,'same_visual_profile_paths':compatible,
            'reference_versions':[{'source':sid,'version':version,'paths':count}
                                  for (sid,version),count in sorted(coverage.items())],
            'query_visual_status':qvis.get('status', 'unprocessed'),
            'status':'candidates' if candidates else 'no_candidate_in_cached_scope' if compatible else 'visual_unavailable',
            'scope':'registered snapshots and same-profile cached fingerprints','candidates':candidates,
            'total_distinct_candidates':len(groups)}


def source_manifest(db, source):
    root = db.execute('SELECT * FROM organize_sources WHERE source_id=?', (source,)).fetchone()
    batch = db.execute("""SELECT * FROM organize_batches WHERE source_id=? AND coverage<>'evidence'
                        ORDER BY batch_id DESC LIMIT 1""",
                       (source,)).fetchone()
    if not root or not batch:
        raise ValueError('Source has no registered inventory')
    files, unavailable = [], []
    for row in db.execute('SELECT relative_path,size,evidence,state FROM organize_files WHERE source_id=? ORDER BY relative_path',
                          (source,)):
        if row['state'] == 'present':
            files.append({'path':row['relative_path'],'size':row['size'],
                          'sha256':json.loads(row['evidence']).get('sha256')})
        elif row['state'] == 'unavailable':
            unavailable.append(row['relative_path'])
    return {'scope':source,'version':batch['version'],'inventory_digest':batch['digest'],
            'source':dict(root),'complete':batch['coverage']=='complete' and not unavailable,
            'unavailable':unavailable,'files':files}


def run(args):
    project = Path(args.config).resolve().parent.parent
    command = args.organize_command
    if command == 'exercise':
        if args.rclone_fixtures:
            from .organize_transfer import exercise as exercise_transfer
            return exercise_transfer(args.rclone_fixtures.resolve(strict=True))
        if args.build_fixtures:
            from .organize_build import exercise_preflight
            return exercise_preflight()
        from .organize_exercise import exercise, exercise_images
        return exercise_images(project/'tools/exiftool/exiftool') if args.image_fixtures else exercise()
    provenance = None
    if command in ('simulate', 'prepare-build', 'reconcile'):
        from .provenance import checkout
        config_path = Path(args.config)
        config = config_path.read_bytes() if config_path.exists() or args.release_tag is not None else None
        config_digest = sha256(config).hexdigest() if config is not None else None
        if (args.release_tag is not None and args.config_sha256 is None
                or args.config_sha256 is not None and args.config_sha256 != config_digest):
            raise ValueError('Formal preflight requires the exact pinned configuration SHA-256')
        provenance = {**checkout(release_tag=args.release_tag), 'config_sha256':config_digest}
    if command == 'reconcile':
        from .organize_reconcile import reconcile
        manifests, checksums = [], {}
        for side,name,expected in (('source',args.source_manifest,args.source_sha256),
                                   ('destination',args.destination_manifest,args.destination_sha256)):
            raw = Path(name).read_bytes()
            checksums[side] = sha256(raw).hexdigest()
            if args.release_tag and expected is None:
                raise ValueError('Formal reconciliation requires both pinned manifest artifact digests')
            if expected is not None and expected!=checksums[side]:
                raise ValueError(f'{side} manifest SHA-256 mismatch')
            manifests.append(json.loads(raw))
        return {**reconcile(*manifests, require_complete_sha=bool(args.release_tag)),
                'manifest_artifact_sha256':checksums,'code_provenance':provenance}
    db_path = audit_path(project, args.database)
    with index.connect(db_path, write=command in ('register','ingest','ingest-evidence')) as db:
        if command in ('verify-evidence','ingest-evidence'):
            path = Path(args.bundle).resolve()
            bundle = json.loads(path.read_text())
            if bundle.get('producer')=='human_review' and path.is_relative_to(project/'reports/datarestore-audit/dsh'):
                raise ValueError('Research output cannot supply human approvals')
            if command=='ingest-evidence':
                return evidence.ingest(db,bundle)
            db.execute('BEGIN')
            return evidence.validate(bundle,index.files(db))
        if command == 'register':
            index.register_sources(db, json.loads(Path(args.config).read_text())['sources'])
            return index.status(db)
        if command == 'ingest':
            data = json.loads(Path(args.manifest).read_text())
            if data.get('coverage') == 'complete' and (data.get('directory_errors') or data.get('truncated')):
                raise ValueError('Incomplete discovery cannot declare complete coverage')
            return index.apply_batch(db, data['source_id'], data['version'], data['rows'],
                                     coverage=data.get('coverage','partial'))
        db.execute('BEGIN')
        if command == 'status':
            return index.status(db)
        if command == 'manifest':
            manifest = source_manifest(db,args.source)
            if args.require_complete_sha:
                from .organize_reconcile import validate
                validate(manifest, require_complete_sha=True)
            return manifest
        if command == 'match-sha':
            from .organize_sha import exact_matches
            return exact_matches(db,args.frozen_manifest,args.manifest_sha256,args.objects)
        if command == 'simulate':
            inputs = {'config.snapshot.json': config}
            if args.release_tag:
                if not args.frozen_manifest or not args.frozen_sha256:
                    raise ValueError('Formal simulation requires --frozen-manifest and --frozen-sha256')
                inputs['frozen-inventory.input.jsonl'] = args.frozen_manifest.read_bytes()
                provenance['frozen_sha256'] = args.frozen_sha256
            elif args.frozen_manifest or args.frozen_sha256:
                raise ValueError('Frozen inventory pinning requires a release tag')
            for name, path in (('reviewed-groups.input.json',args.reviewed_groups),
                               ('candidate-queries.input.json',args.candidate_queries)):
                if path:
                    inputs[name] = Path(path).read_bytes()
            reviewed = json.loads(inputs.get('reviewed-groups.input.json',b'[]'))
            queries = json.loads(inputs.get('candidate-queries.input.json',b'[]'))
            return output_plan(db, audit_path(project, args.output), reviewed, queries,
                               provenance=provenance, inputs=inputs if args.release_tag else None)
        if command == 'prepare-build':
            from .organize_build import prepare
            plan = Path(args.plan)
            disposition = plan/'inventory-disposition.jsonl'
            expected = None
            if args.release_tag:
                summary = json.loads((plan/'summary.json').read_bytes())
                sealed_code = summary.get('code_provenance') or {}
                if (summary.get('mode') != 'formal_preflight' or
                        {k:v for k,v in sealed_code.items() if k != 'frozen_sha256'} != provenance):
                    raise ValueError('Formal preflight requires a formal simulation at the same release and config')
                for name, digest in (('config.snapshot.json', config_digest),
                                     ('frozen-inventory.input.jsonl', sealed_code.get('frozen_sha256'))):
                    if checksum(plan/name) != digest:
                        raise ValueError(f'Formal simulation input changed: {name}')
                provenance = sealed_code
                expected = summary['disposition_sha256']
                if not index.SHA256.fullmatch(expected):
                    raise ValueError('Formal simulation disposition SHA-256 is invalid')
            return prepare(index.files(db), disposition, audit_path(project,args.output),
                           provenance=provenance, expected_disposition_sha256=expected)
        rows = index.files(db)
        if command == 'lookup':
            registered = {row[0] for row in db.execute('SELECT source_id FROM organize_sources')}
            if not set(args.reference_source) <= registered:
                raise ValueError('Unknown reference source')
            return lookup(rows, [args.source,args.path], set(args.reference_source), args.top_k)
        group = json.loads(Path(args.group).read_text())
        by_id = {tuple(identity(row)):row for row in rows}
        return recommendation([by_id[tuple(item)] for item in group['members']], group['relation'],
                              group.get('reviewed_pairs', ()))
