#!/usr/bin/env python3
"""Reproducible synthetic positive/boundary evidence; never opens the NAS config."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
import test_similarity as fixtures
from PIL import Image, ImageDraw, ImageEnhance, PngImagePlugin
from nas_photo import similarity as sim
from nas_photo import duplicates as exact


def main():
    root=Path(__file__).resolve().parents[1]
    fixture=fixtures.SimilarityTests();fixture.setUp()
    try:
        im=fixture.im
        info=PngImagePlugin.PngInfo();info.add_text('Comment','synthetic metadata variant')
        burst=im.copy();ImageDraw.Draw(burst).ellipse((350,190,390,230),fill='yellow')
        variants=[('reference.png',im,'PNG',{}),('binary-copy.png',im,'PNG',{}),
                  ('metadata.png',im,'PNG',{'pnginfo':info}),('q95.jpg',im,'JPEG',{'quality':95}),
                  ('q60.jpg',im,'JPEG',{'quality':60}),('small.png',im.resize((300,200),Image.Resampling.LANCZOS),'PNG',{}),
                  ('rotated.png',im.transpose(Image.Transpose.ROTATE_90),'PNG',{}),
                  ('bright.png',ImageEnhance.Brightness(im).enhance(1.05),'PNG',{}),
                  ('crop-light.png',im.crop((6,4,594,396)),'PNG',{}),('crop-large.png',im.crop((140,90,460,310)),'PNG',{}),
                  ('synthetic-burst.png',burst,'PNG',{}),('solid-red.png',Image.new('RGB',(600,400),'red'),'PNG',{}),
                  ('solid-green.png',Image.new('RGB',(600,400),'green'),'PNG',{})]
        items=[fixture.seed(name,img,fmt,**kw) for name,img,fmt,kw in variants]
        hj=exact.create_hash_job(fixture.config,items,exact.options(fixture.config,include_non_candidates=True))
        binary=exact.run_hash_job(fixture.config,hj)
        jid,analysis=fixture.run_visual(items)
        comparison=sim.compare(fixture.config,jid);report=sim.report(fixture.config,jid)
        reference=report['members'][0];cases=[]
        for m in report['members'][1:]:
            evidence,reason=sim.pair_evidence(reference['visual'],m['visual'],sim.comparison_options())
            cases.append({'variant':m['relative_path'],'pixel_equal':reference['visual']['pixel_sha256']==m['visual']['pixel_sha256'],
                          'visual_candidate':evidence is not None and reference['visual']['pixel_sha256']!=m['visual']['pixel_sha256'],
                          'raw_perceptual_threshold_pass':evidence is not None,'candidate_evidence':evidence,'rejection_reason':reason})
        summary={'kind':'local_synthetic_only_not_NAS','cases':cases,'binary_hash_job':binary,'visual_job':analysis,
                 'comparison':comparison,'note':'Crop results are measured on one synthetic scene, not a calibrated recall estimate. Synthetic burst remains candidate only.'}
        (root/'reports/phase3-local-validation.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
        (root/'reports/phase3-local-positive.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'synthetic_files':len(items),'L1_groups':report['pixel_equivalent_group_count'],
                          'L2_candidates':report['visual_candidate_count'],'cases':[{k:c[k] for k in ('variant','pixel_equal','visual_candidate')} for c in cases]},ensure_ascii=False))
    finally:fixture.tearDown()

if __name__=='__main__':main()
