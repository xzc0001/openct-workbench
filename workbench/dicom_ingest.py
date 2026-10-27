"""Strict CT Part 10 ingestion, bounded parsing and ZIP extraction without names."""
from pathlib import Path, PurePosixPath
import io
import math
import os
import shutil
import stat
import struct
import uuid
import warnings
import zipfile

MAX_FILE = 64 * 1024**2
MAX_HEADER = 2 * 1024**2
MAX_FILES = 20000
MAX_EXPANDED = 8 * 1024**3
CT = '1.2.840.10008.5.1.4.1.1.2'
SYNTAXES = {'1.2.840.10008.1.2','1.2.840.10008.1.2.1','1.2.840.10008.1.2.2',
            *('1.2.840.10008.1.2.4.'+n for n in ['50','51','57','70','80','81','90','91']),
            '1.2.840.10008.1.2.5'}


class Rejected(ValueError):
    pass


class HeaderFile:
    """Never let metadata parsing allocate/read into a large pixel payload."""
    def __init__(self, stream):self.stream=stream
    def tell(self):return self.stream.tell()
    def read(self, size=-1):
        if size<0 or size>MAX_HEADER or self.tell()+size>MAX_HEADER:raise Rejected('DICOM 文件头过大')
        return self.stream.read(size)
    def seek(self, offset, whence=0):
        target=offset if whence==0 else self.tell()+offset if whence==1 else MAX_HEADER+1
        if not 0<=target<=MAX_HEADER:raise Rejected('DICOM 文件头超出限制')
        return self.stream.seek(offset,whence)


def validate(path):
    import pydicom
    from pydicom.filereader import _read_file_meta_info
    try:
        size=Path(path).stat().st_size
        if not 132<size<=MAX_FILE:raise Rejected('DICOM 文件大小超出限制')
        with Path(path).open('rb') as source, warnings.catch_warnings():
            warnings.simplefilter('error')
            # Part 10 preambles can hide executable/polyglot files. Accept only
            # the conventional zero-filled CT preamble, never guess via force=True.
            if source.read(132)!=b'\0'*128+b'DICM':raise Rejected('不是支持的 DICOM CT 文件')
            meta=_read_file_meta_info(HeaderFile(source))
            syntax=str(meta.get('TransferSyntaxUID',''))
            if syntax not in SYNTAXES or str(meta.get('MediaStorageSOPClassUID',''))!=CT:
                raise Rejected('不支持的 DICOM 对象或传输语法')
            source.seek(0)
            fields=['SOPClassUID','SOPInstanceUID','StudyInstanceUID','SeriesInstanceUID','FrameOfReferenceUID',
                    'Modality','Rows','Columns','NumberOfFrames','SamplesPerPixel','PhotometricInterpretation',
                    'BitsAllocated','BitsStored','HighBit','PixelRepresentation','ImagePositionPatient',
                    'ImageOrientationPatient','PixelSpacing']
            header=pydicom.dcmread(HeaderFile(source),stop_before_pixels=True,specific_tags=fields,force=False)
            if str(header.get('SOPClassUID',''))!=CT or header.get('Modality')!='CT' or int(header.get('NumberOfFrames',1))!=1:
                raise Rejected('仅支持常规单帧 CT DICOM')
            for key in ['SOPInstanceUID','StudyInstanceUID','SeriesInstanceUID','FrameOfReferenceUID']:
                value=str(header.get(key,''))
                if not value or not pydicom.uid.UID(value).is_valid:raise Rejected('DICOM UID 无效')
            if str(meta.get('MediaStorageSOPInstanceUID',''))!=str(header.SOPInstanceUID):raise Rejected('DICOM 对象标识不一致')
            rows,cols=int(header.Rows),int(header.Columns)
            bits,stored=int(header.BitsAllocated),int(header.BitsStored)
            if not (1<=rows<=4096 and 1<=cols<=4096 and bits in {8,16} and 1<=stored<=bits and int(header.HighBit)==stored-1 and int(header.PixelRepresentation) in {0,1} and int(header.SamplesPerPixel)==1 and str(header.PhotometricInterpretation) in {'MONOCHROME1','MONOCHROME2'}):
                raise Rejected('CT 像素参数无效')
            for key,count in [('ImagePositionPatient',3),('ImageOrientationPatient',6),('PixelSpacing',2)]:
                values=[float(v) for v in getattr(header,key)]
                if len(values)!=count or not all(math.isfinite(v) for v in values) or (key=='PixelSpacing' and min(values)<=0):
                    raise Rejected('CT 空间参数无效')
            endian='>' if syntax=='1.2.840.10008.1.2.2' else '<'
            if source.read(4)!=struct.pack(endian+'HH',0x7FE0,0x0010):raise Rejected('缺少 CT 像素数据')
            if syntax=='1.2.840.10008.1.2':length=struct.unpack(endian+'I',source.read(4))[0]
            else:
                if source.read(2) not in {b'OB',b'OW'} or source.read(2)!=b'\0\0':raise Rejected('CT 像素数据格式无效')
                length=struct.unpack(endian+'I',source.read(4))[0]
            compressed=syntax not in {'1.2.840.10008.1.2','1.2.840.10008.1.2.1','1.2.840.10008.1.2.2'}
            if not compressed:
                expected=rows*cols*(bits//8);expected+=expected%2
                if length!=expected or source.tell()+length>size:raise Rejected('CT 像素长度不完整或不匹配')
                source.seek(length,1)
            else:
                if length!=0xFFFFFFFF:raise Rejected('压缩 CT 像素封装无效')
                fragments=0
                while source.tell()<size:
                    tag,count=struct.unpack('<II',source.read(8))
                    if tag==0xE0DDFFFE and count==0:break
                    if tag!=0xE000FFFE or count%2 or count>MAX_FILE or source.tell()+count>size:
                        raise Rejected('压缩 CT 像素片段无效')
                    source.seek(count,1);fragments+=1
                    if fragments>10000:raise Rejected('压缩 CT 像素片段过多')
                else:raise Rejected('压缩 CT 像素不完整')
                if fragments<2:raise Rejected('压缩 CT 缺少像素片段')
            # Only standard trailing padding is accepted after the pixel data.
            if source.tell()!=size:
                tag=source.read(4)
                if tag!=struct.pack(endian+'HH',0xFFFC,0xFFFC):raise Rejected('DICOM 尾部存在额外内容')
                if syntax!='1.2.840.10008.1.2':
                    if source.read(4)!=b'OB\0\0':raise Rejected('DICOM 尾部填充无效')
                count=struct.unpack(endian+'I',source.read(4))[0]
                if count>MAX_HEADER or source.tell()+count!=size or any(source.read(count)):raise Rejected('DICOM 尾部填充无效')
        return True
    except Rejected:raise
    except Exception as error:
        raise Rejected('DICOM 内容校验失败') from error


def accept(path, folder):
    try:
        validate(path)
        destination=Path(folder)/(uuid.uuid4().hex+'.dcm')
        os.replace(path,destination)
        return destination
    finally:
        Path(path).unlink(missing_ok=True)


def extract(archive_path, folder, callback=None):
    """Discard unrelated files; never use any archive path as a disk path."""
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    accepted=rejected=0
    try:
        # ZipFile builds its entry list eagerly. Bound the central directory
        # before asking it to allocate an attacker-controlled number of entries.
        with Path(archive_path).open('rb') as source:
            record=zipfile._EndRecData(source)
        if not record or record[zipfile._ECD_ENTRIES_TOTAL]>MAX_FILES or record[zipfile._ECD_SIZE]>16*1024**2:
            raise Rejected('ZIP 目录无效或条目过多')
        with zipfile.ZipFile(archive_path) as archive:
            members=[m for m in archive.infolist() if not m.is_dir()]
            total=sum(m.file_size for m in members)
            if len(members)>MAX_FILES or total>MAX_EXPANDED or shutil.disk_usage(folder).free<total+2*1024**3:
                raise Rejected('ZIP 文件数、展开大小或磁盘空间超出限制')
            for i,member in enumerate(members):
                name=PurePosixPath(member.filename.replace('\\','/'))
                mode=member.external_attr>>16
                unsafe=(name.is_absolute() or '..' in name.parts or ':' in member.filename or '\0' in member.filename
                        or (stat.S_IFMT(mode) not in {0,stat.S_IFREG}) or member.flag_bits&1
                        or not 132<member.file_size<=MAX_FILE or member.compress_type not in {zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED}
                        or member.file_size>max(1,member.compress_size)*2000)
                if unsafe:rejected+=1;continue
                temporary=folder.parent/(uuid.uuid4().hex+'.ingest')
                try:
                    with archive.open(member) as source:
                        preamble=source.read(132)
                        if preamble!=b'\0'*128+b'DICM':raise Rejected('不是 DICOM')
                        with temporary.open('xb') as target:
                            target.write(preamble);count=132
                            while block:=source.read(min(1024**2,MAX_FILE-count+1)):
                                count+=len(block)
                                if count>MAX_FILE:raise Rejected('单文件过大')
                                target.write(block)
                        if count!=member.file_size:raise Rejected('ZIP 长度不匹配')
                    accept(temporary,folder);accepted+=1
                except (Rejected,zipfile.BadZipFile,RuntimeError,EOFError):rejected+=1
                finally:temporary.unlink(missing_ok=True)
                if callback and (i%50==0 or i==len(members)-1):callback(i+1,len(members))
        return {'accepted':accepted,'rejected':rejected}
    finally:
        # Never retain a ZIP containing discarded scripts or other unrelated files.
        Path(archive_path).unlink(missing_ok=True)
