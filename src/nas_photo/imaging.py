"""Deterministic full-pixel and perceptual evidence, executed only in an isolated worker."""
import hashlib
from importlib.metadata import version, PackageNotFoundError
import io
import json
import os
import resource
import time
import warnings
from .metadata import secure_path, snapshot, fingerprint
from .hashing import read_chunk

NORMALIZATION = "rgba8-icc-srgb-exif-v1"


def pipeline():
    packages = {}
    for name in ('Pillow', 'pillow-heif', 'ImageHash', 'numpy', 'scipy'):
        try: packages[name] = version(name)
        except PackageNotFoundError: raise ValueError("Phase 3 dependencies missing; install requirements-visual.txt in the project venv")
    # Include codec builds as well as Python packages in the cache/equality domain.
    from PIL import features
    import pillow_heif
    return {"normalization": NORMALIZATION, "packages": packages,
            "codecs": {k: features.version(k) for k in ('jpg', 'zlib', 'littlecms2')},
            "heif": pillow_heif.libheif_info(), "pixel_format": "RGBA8-row-major",
            "orientation": "Pillow EXIF transpose; HEIF plugin applies container/EXIF orientation on open",
            "color": "ICC to sRGB relative-colorimetric; untagged RGB/L/P assumed sRGB; untagged CMYK rejected; HEIF libheif RGB8 conversion; HDR rejected",
            "alpha": "L1 preserve RGBA including hidden RGB; L2 composite on opaque white",
            "perceptual": {"main": "ImageHash.phash", "auxiliary": "ImageHash.dhash", "bits": 64,
                           "hash_size": 8, "highfreq_factor": 4, "resize": "LANCZOS",
                           "pre_resize": 256, "rotations_degrees_ccw": [0,90,180,270],
                           "distance": "Hamming"}}


def pipeline_key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Limited(Exception): pass
class Unsupported(Exception): pass


def analyze_file(req):
    total = 0
    fd = counter = None
    before = None
    started = time.monotonic()
    try:
        from PIL import Image, ImageCms, ImageOps, ImageStat
        import imagehash
        import pillow_heif
        Image.MAX_IMAGE_PIXELS = req['max_pixels']
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        pillow_heif.register_heif_opener(decode_threads=1, thumbnails=False, depth_images=False, aux_images=False)
        if pipeline_key(pipeline()) != req['pipeline_key']: raise ValueError('decoder_version_changed')
        path = secure_path(req['root'], req['relative_path'])
        fs, identity = snapshot(path)
        before = {"fs": fs, "identity": list(identity)}
        if fingerprint(fs) != req['expected_fingerprint'] or list(identity) != req['expected_identity']:
            raise ValueError('file_changed_since_probe')
        if fs['size'] > req['max_file_bytes'] or fs['size'] > req['read_allowance']: raise Limited('file_size_limit')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        opened = os.fstat(fd)
        if (opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns) != identity: raise ValueError('file_replaced_before_open')
        counter = os.open(req['progress_path'], os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0))
        # One bounded source pass. All decoder seeks use this in-memory buffer, never the NAS.
        buffer = io.BytesIO()
        file_sha = hashlib.sha256()
        while total < fs['size']:
            block = read_chunk(fd, min(1024 * 1024, fs['size'] - total))
            if not block: raise OSError('unexpected_eof')
            total += len(block); os.pwrite(counter, f'{total:024d}'.encode(), 0)
            buffer.write(block); file_sha.update(block)
        buffer.seek(0)
        decode_start = time.monotonic()
        with Image.open(buffer, formats=['JPEG','PNG','HEIF']) as image:
            fmt, mode, source_size = image.format, image.mode, list(image.size)
            w,h = image.size
            if w*h > req['max_pixels'] or max(w,h)>req['max_dimension']: raise Limited('pixel_or_dimension_limit')
            # Conservative working-set estimate, backed by the supervisor's measured RSS ceiling.
            if w*h*24 + fs['size']*2 + 96*1024**2 > req['max_rss_mb']*1024**2: raise Limited('estimated_memory_limit')
            if fmt != 'HEIF' and getattr(image, 'n_frames', 1) != 1: raise Unsupported('animated_or_multiframe_image')
            bitdepth = image.info.get('bit_depth', 8)
            nclx = image.info.get('nclx_profile') or {}
            if bitdepth > 8 or nclx.get('transfer_characteristics') in (16,18): raise Unsupported('high_bit_depth_or_hdr')
            if mode not in ('RGB','RGBA','L','LA','P','CMYK'): raise Unsupported('unsupported_color_mode:'+mode)
            orientation = image.info.get('original_orientation', image.getexif().get(274,1))
            profile = image.info.get('icc_profile')
            image.load()
            oriented = ImageOps.exif_transpose(image)
            rgba = oriented.convert('RGBA')
            color_action = 'assumed_sRGB' if not profile else 'ICC_to_sRGB'
            if profile:
                source = oriented if mode == 'CMYK' else oriented.convert('RGB')
                converted = ImageCms.profileToProfile(source, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                              ImageCms.createProfile('sRGB'), renderingIntent=1, outputMode='RGB')
                converted.putalpha(rgba.getchannel('A')); rgba.close(); rgba = converted
            elif mode == 'CMYK': raise Unsupported('untagged_CMYK')
            decode_seconds = time.monotonic() - decode_start
            fingerprint_start = time.monotonic()
            width,height = rgba.size
            sha = hashlib.sha256(f'RGBA8:{width}x{height}:'.encode())
            for top in range(0, height, 64):
                with rgba.crop((0,top,width,min(top+64,height))) as strip: sha.update(strip.tobytes())
            # Reduce before float/DCT work; never allocate a full-size float image.
            small = rgba.copy(); small.thumbnail((256,256), Image.Resampling.LANCZOS)
            display = Image.new('RGBA', small.size, (255,255,255,255))
            display.alpha_composite(small); display = display.convert('RGB')
            stat = ImageStat.Stat(display); gray = display.convert('L')
            descriptors = []
            for angle in (0,90,180,270):
                rotated = display.rotate(angle, expand=True)
                descriptors.append({'rotation': angle, 'phash': str(imagehash.phash(rotated, hash_size=8, highfreq_factor=4)),
                                    'dhash': str(imagehash.dhash(rotated, hash_size=8))})
                rotated.close()
            result = {'format': fmt, 'source_mode': mode, 'source_dimensions': source_size,
                      'width': width, 'height': height, 'orientation_original': orientation,
                      'orientation_action': 'HEIF_plugin_then_exif_transpose' if fmt=='HEIF' else 'exif_transpose',
                      'color_action': color_action, 'icc_sha256': hashlib.sha256(profile).hexdigest() if profile else None,
                      'pixel_sha256': sha.hexdigest(), 'perceptual': descriptors, 'hash_bits': 64,
                      'rgb_mean': stat.mean, 'luma_stddev': ImageStat.Stat(gray).stddev[0],
                      'decoder': req['pipeline'], 'decode_seconds': decode_seconds,
                      'fingerprint_seconds': time.monotonic()-fingerprint_start,
                      'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if os.uname().sysname=='Darwin' else 1024),
                      'file_sha256_from_read': file_sha.hexdigest()}
            gray.close(); display.close(); small.close(); rgba.close(); oriented.close()
        after_fs, after_identity = snapshot(path); current_fd = os.fstat(fd)
        if (after_identity != identity or fingerprint(after_fs)!=fingerprint(fs) or after_fs['ctime']!=fs['ctime']
            or (current_fd.st_dev,current_fd.st_ino,current_fd.st_size,current_fd.st_mtime_ns)!=identity):
            raise ValueError('file_changed_during_visual_analysis')
        return {'completed': True, 'status': 'valid', 'fingerprint': fingerprint(fs), 'before': before,
                'after': {'fs':after_fs,'identity':list(after_identity)}, 'result': result, 'read_bytes':total}
    except Exception as e:
        status = 'limited' if isinstance(e, Limited) or type(e).__name__.startswith('DecompressionBomb') else 'unsupported' if isinstance(e, Unsupported) else 'failed'
        return {'completed': False, 'status':status, 'read_bytes':total, 'before':before,
                'error': {'code': type(e).__name__, 'message':str(e)[:500]}, 'elapsed_seconds':time.monotonic()-started}
    finally:
        if fd is not None: os.close(fd)
        if counter is not None: os.close(counter)
