#!/usr/bin/env python3
"""Build a separately identified strict-TLS candidate from the pinned Zuul wheel."""

import argparse
import base64
import csv
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

from atomic_file import atomic_write_text
from bounded_file import read_bounded_bytes

ROOT=Path(__file__).resolve().parents[1]
SOURCE_SHA="54b8cfcf397ce7324a87b3c0cfea5a8fcd0f430082ba6be25f9e62326d9949e2"
TARGET="zuul/zk/__init__.py"
PROFILE="14.3.0-zookeeper-strict-tls-v1"


def modified(payload):
    needle=b"                args['ca'] = self.tls_ca\n"
    if payload.count(needle)!=1:raise ValueError('Unexpected original Zuul TLS client source')
    return payload.replace(needle,needle+b"                args['verify_certs'] = True\n                args['check_hostname'] = True\n")


def build(source,output):
    payload=read_bounded_bytes(source,max_bytes=16*1024**2)
    if hashlib.sha256(payload).hexdigest()!=SOURCE_SHA:raise ValueError('Require the exact original upstream Zuul 14.3.0 wheel')
    if output.exists() or output.is_symlink() or not output.resolve().is_relative_to((ROOT/'private').resolve()):raise ValueError('Require a fresh private candidate directory')
    with zipfile.ZipFile(io.BytesIO(payload)) as original:
        files={info.filename:original.read(info.filename) for info in original.infolist() if not info.is_dir()}
        if any(Path(name).is_absolute() or '..' in Path(name).parts for name in files):raise ValueError('Unsafe original wheel member')
        old=files[TARGET];files[TARGET]=modified(old)
        record='zuul-14.3.0.dist-info/RECORD';notice='zuul-14.3.0.dist-info/PLATFORM_ZK_TLS_CHANGE.txt'
        files[notice]=(PROFILE+'\nDownstream candidate: require certificate-chain and hostname verification for ZooKeeper TLS.\nOriginal upstream wheel SHA256: '+SOURCE_SHA+'\nOriginal package version/CLI version retained; this is not the unmodified upstream artifact.\n').encode()
        rows=[]
        for name in sorted(files):
            if name!=record:rows.append([name,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).decode().rstrip('='),str(len(files[name]))])
        rows.append([record,'','']);text=io.StringIO();csv.writer(text,lineterminator='\n').writerows(rows);files[record]=text.getvalue().encode()
        output.mkdir(mode=0o700,parents=True);wheel=output/'zuul-14.3.0-py3-none-any.whl'
        with zipfile.ZipFile(wheel,'w') as candidate:
            for info in original.infolist():
                if not info.is_dir():candidate.writestr(info,files[info.filename])
            info=zipfile.ZipInfo(notice,date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16;candidate.writestr(info,files[notice])
    candidate = read_bounded_bytes(wheel, max_bytes=16 * 1024 ** 2)
    report={'profile':PROFILE,'source_wheel_sha256':SOURCE_SHA,'candidate_wheel_sha256':hashlib.sha256(candidate).hexdigest(),'original_client_sha256':hashlib.sha256(old).hexdigest(),'candidate_client_sha256':hashlib.sha256(files[TARGET]).hexdigest(),'modified_program_files':[TARGET],'wheel_record_updated':True,'upstream_unmodified_release':False,'live_verified':False}
    atomic_write_text(output / 'build-report.json', json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source-wheel',type=Path,required=True);parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args()
    try:report=build(args.source_wheel,args.output_dir)
    except (ValueError,OSError,zipfile.BadZipFile):print('Strict-TLS candidate build stopped; review private inputs.',file=sys.stderr);return 1
    print(json.dumps(report));return 0


if __name__=='__main__':raise SystemExit(main())
