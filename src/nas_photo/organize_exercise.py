"""Isolated copy, collision and recovery rehearsal. No production execution entry point."""
from hashlib import file_digest, sha256
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
import os
import shutil
import stat
import subprocess

from .config import relative
from .metadata import secure_path
from .organize_decisions import simulation
from .organize_reconcile import reconcile


def checksum(path):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def verified_copy(source, root, destination, expected_digest, *, expected_stat=None, copy_bytes=shutil.copyfileobj):
    """Publish a verified file without replacing an existing destination."""
    parts = Path(relative(destination)).parts
    folder = root
    for part in parts[:-1]:
        folder = folder / part
        if folder.is_symlink():
            raise ValueError('Destination contains a symlink')
        folder.mkdir(exist_ok=True)
    target = folder / parts[-1]
    source_before = source.stat(follow_symlinks=False)
    revision = (source_before.st_size,source_before.st_mtime_ns,source_before.st_ctime_ns)
    if not stat.S_ISREG(source_before.st_mode) or (expected_stat is not None and tuple(expected_stat)!=revision):
        raise ValueError('Frozen source revision changed')
    if target.is_symlink():
        raise ValueError('Destination is a symlink')
    if target.exists():
        if target.is_file() and checksum(target) == expected_digest:
            return 'already_verified'
        raise FileExistsError('Destination has different content')
    with NamedTemporaryFile(prefix='.organize-stage-', dir=folder) as pending:
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW),'rb') as stream:
            copy_bytes(stream, pending)
            after = os.fstat(stream.fileno())
            final = source.stat(follow_symlinks=False)
            for observed in (after,final):
                if ((observed.st_size,observed.st_mtime_ns,observed.st_ctime_ns)!=revision
                        or (observed.st_dev,observed.st_ino)!=(source_before.st_dev,source_before.st_ino)):
                    raise ValueError('Source revision changed during copy')
        pending.flush()
        os.fsync(pending.fileno())
        if checksum(Path(pending.name)) != expected_digest:
            raise ValueError('Staging copy failed SHA-256 verification')
        try:
            os.link(pending.name, target)
        except FileExistsError:
            if not target.is_symlink() and target.is_file() and checksum(target) == expected_digest:
                return 'already_verified'
            raise
        directory_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return 'copied_verified'


def exercise():
    actions = []
    with TemporaryDirectory(prefix='nas-photo-rehearsal-') as tmp:
        base = Path(tmp)
        source, output, recovery = (base / name for name in ('input', 'library', 'recovery'))
        for folder in (source, output, recovery):
            folder.mkdir()
        a, b = b'generated fixture 1', b'generated fixture 2'
        for name, data in (('first.jpg', a), ('second.jpg', b)):
            (source/name).write_bytes(data)
        indexed = [{'source_id':'album','relative_path':name,'size':len(data),'version':'fixture-v1',
                    'state':'present','evidence':{'sha256':sha256(data).hexdigest()}}
                   for name,data in (('first.jpg',a),('second.jpg',b))]
        records, _, plan_summary = simulation(indexed)
        manifest = [(r['source'][1],r['proposed_destination'],r['sha256']) for r in records]
        first, second = manifest

        def execute(row, **options):
            src, dest, digest = row
            return verified_copy(secure_path(source, src), output, dest, digest, **options)

        def interrupt(inp, out):
            out.write(inp.read(5))
            raise InterruptedError('simulated interruption after partial write')

        actions.append({'operation':'copy_first', 'status':execute(first)})
        try:
            execute(second, copy_bytes=interrupt)
        except InterruptedError:
            assert not (output / second[1]).exists()
            actions.append({'operation':'interruption', 'status':'partial_not_published'})
        else:
            raise AssertionError('Interruption was not exercised')
        actions.append({'operation':'resume_first', 'status':execute(first)})
        actions.append({'operation':'retry_second', 'status':execute(second)})
        try:
            execute((second[0], first[1], second[2]))
        except FileExistsError:
            actions.append({'operation':'name_collision', 'status':'preserved_existing_content'})
        else:
            raise AssertionError('Collision was not exercised')
        for _, name, digest in manifest:
            asset = secure_path(output, name)
            if checksum(asset) != digest:
                raise AssertionError('Library verification failed')
            verified_copy(asset, recovery, name, digest)
            if checksum(secure_path(recovery, name)) != digest:
                raise AssertionError('Recovery verification failed')
        actions.append({'operation':'isolated_restore','status':'all_sha256_verified'})
        def captured(scope, folder):
            return {'scope':scope,'version':'fixture-v1','complete':True,'files':[
                {'path':name,'size':(folder/name).stat().st_size,'sha256':checksum(folder/name)}
                for _,name,_ in manifest]}
        agreement = reconcile(captured('staged_us',output),captured('restored_cn',recovery))
        if agreement['counts'] != {'verified_equal':len(manifest)}:
            raise AssertionError('Isolated reconciliation failed')
    return {'status':'completed','scope':'generated temporary files','operations':actions,
            'library_files':len(manifest),'recovered_files':len(manifest),'plan':plan_summary,
            'reconciliation_counts':agreement['counts']}


def exercise_images(exiftool):
    """Rehearse collision allocation, scoped rollback and EXIF on generated JPEGs."""
    from PIL import Image

    with TemporaryDirectory(prefix='nas-photo-image-rehearsal-') as tmp:
        root = Path(tmp)
        source, stage, library, archive, previous = (root/name for name in
            ('input','staging','generation','archived-generation','previous-generation'))
        for folder in (source,stage,library,previous):
            folder.mkdir()
        anchor = previous/'keep.txt'
        anchor.write_text('prior generation')
        prior_hash = checksum(anchor)
        first, second = source/'original-a.jpg',source/'original-b.jpg'
        for path,color in ((first,(20,60,140)),(second,(140,20,60))):
            Image.new('RGB',(37,29),color).save(path,quality=91)
        original_hashes = {p.name:checksum(p) for p in (first,second)}
        edited = stage/'metadata-copy.jpg'
        verified_copy(first,stage,edited.name,original_hashes[first.name])
        subprocess.run([str(exiftool),'-overwrite_original','-DateTimeOriginal=2019:04:05 12:30:40',str(edited)],
                       capture_output=True,check=True,timeout=30)
        import json
        tag = json.loads(subprocess.check_output([str(exiftool),'-json','-DateTimeOriginal',str(edited)],timeout=30))
        if tag[0]['DateTimeOriginal']!='2019:04:05 12:30:40':
            raise AssertionError('EXIF readback failed')
        with Image.open(first) as a, Image.open(edited) as b:
            if (a.size,a.mode,a.tobytes())!=(b.size,b.mode,b.tobytes()):
                raise AssertionError('EXIF edit altered decoded pixels')
        inputs = {'Photo.jpg':edited,'photo.JPG':second}
        rows = [{'source_id':'album','relative_path':name,'size':path.stat().st_size,'state':'present',
                 'version':'isolated-jpeg-v1','evidence':{'sha256':checksum(path)}} for name,path in inputs.items()]
        records,_,plan = simulation(rows)
        if plan['destination_renames']!=2:
            raise AssertionError('Portable conflict did not exercise resolution')
        first_record,second_record = records
        def publish(record, **options):
            return verified_copy(inputs[record['source'][1]],library,record['proposed_destination'],
                                 record['sha256'],**options)
        publish(first_record)
        def fail(inp,out):
            out.write(inp.read(13))
            raise InterruptedError('isolated interruption')
        try:
            publish(second_record,copy_bytes=fail)
        except InterruptedError:
            if (library/second_record['proposed_destination']).exists():
                raise AssertionError('Partial file was published')
        else:
            raise AssertionError('Expected interruption')
        resumed = publish(first_record)
        publish(second_record)
        def verify(folder):
            actual = {p.relative_to(folder).as_posix():checksum(p) for p in folder.rglob('*') if p.is_file()}
            expected = {r['proposed_destination']:r['sha256'] for r in records}
            if actual!=expected:
                raise AssertionError('Manifest and entity files disagree')
        verify(library)
        library.rename(archive)
        if library.exists() or checksum(anchor)!=prior_hash:
            raise AssertionError('Rollback changed the previous generation')
        library.mkdir()
        for record in records:
            target = record['proposed_destination']
            verified_copy(secure_path(archive,target),library,target,record['sha256'])
        verify(library)
        if any(checksum(source/name)!=digest for name,digest in original_hashes.items()):
            raise AssertionError('Source fixture changed')
    return {'status':'completed','scope':'generated JPEGs in a temporary directory','manifest_files':len(records),
            'portable_renames':plan['destination_renames'],'interrupted_publication':'partial_not_published',
            'resume_first':resumed,'copy_sha_and_manifest':'verified','metadata_date_readback':True,
            'decoded_pixels_identical':True,'rollback':'new_generation_archived_previous_unchanged',
            'restore_manifest':'verified','execution_allowed':False}
