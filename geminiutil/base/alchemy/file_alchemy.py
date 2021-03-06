import os
import logging
import hashlib

from sqlalchemy.orm import object_session, relationship
from sqlalchemy import Column, event, ForeignKey

from astropy.io import fits

from sqlalchemy import Integer, String

import geminiutil

from geminiutil.base.alchemy.base import Base
from geminiutil.util import hashfile

logger = logging.getLogger(__name__)

class FITSClassifyError(ValueError):
    pass




class DataPathMixin(object):

    work_dir = ''
    process_dirs = {}

class AbstractFileTable(Base, DataPathMixin):
    """
    Abstract table for basic files


    Columns
    -------

    fname: str
        filename or path

    path: str
        relative path to work_dir

    size: int
        size in bytes

    md5: str
        md5 is the hex

    """
    __abstract__ = True

    id = Column(Integer, primary_key=True)
    fname = Column(String)
    path = Column(String)
    size = Column(Integer)
    md5 = Column(String)


    @classmethod
    def from_file(cls, full_fname, use_abspath=False):
        fname = os.path.basename(full_fname)
        if use_abspath:
            logger.warn('Using absolute paths is now discouraged - all files should be located in one workdirectory')
            path = os.path.abspath(os.path.dirname(full_fname))
        else:
            path = os.path.dirname(full_fname)

        filesize = os.path.getsize(full_fname)
        md5_hash = hashfile(file(full_fname, 'rb'), hashlib.md5())

        return cls(fname=fname, path=path, size=filesize, md5=md5_hash)


    @property
    def full_path(self):
        return os.path.join(self.work_dir, self.path, self.fname)

    @staticmethod
    def remove_file_before_delete(mapper, connection, target):
        logger.info('Deleting file {0}'.format(target.full_path))
        os.remove(target.full_path)


    @classmethod
    def register(cls):
        "Registering the deletion event"
        event.listen(cls, 'before_delete', cls.remove_file_before_delete)



class AbstractCalibrationFileTable(AbstractFileTable):
    __abstract__ = True

    work_dir = os.path.join(geminiutil.__path__[0], 'data')

class FITSFile(AbstractFileTable):
    __tablename__ = 'fits_file'

    extensions = Column(Integer)



    @classmethod
    def from_fits_file(cls, full_fname, use_abspath=False):
        extensions = len(fits.open(full_fname))

        fits_obj = cls.from_file(full_fname, use_abspath=use_abspath)
        fits_obj.extensions = extensions
        return fits_obj



    @property
    def fits_data(self):
        return fits.open(self.full_path)

    @property
    def header(self):
        return fits.getheader(self.full_path)

    @property
    def data(self):
        return fits.getdata(self.full_path)


    @property
    def shape(self):
        return self.header['naxis1'], self.header['naxis2']


    def __init__(self, fname, path, size, md5, extensions=None):
        self.fname = fname
        self.path = path
        self.size = size
        self.md5 = md5
        self.extensions = extensions

    def __repr__(self):
        return "<FITS file ID {0:d} @ {1}>".format(self.id, self.full_path)

FITSFile.register()


class DataFile(AbstractFileTable, DataPathMixin):
    __tablename__ = 'data_files'

class TemporaryFITSFile(Base):
    __tablename__ = 'temporary_fits_files'

    id = Column(Integer, primary_key=True)
    data_file_id = Column(Integer, ForeignKey('data_files.id'))
    extensions = Column(Integer)

    data_file = relationship('DataFile', uselist=False)

    @property
    def fname(self):
        return self.data_file.fname


    @property
    def fits_data(self):
        return fits.open(self.data_file.full_path, ignore_missing_end=True)

    @property
    def header(self):
        return fits.getheader(self.data_file.full_path, ignore_missing_end=True)

    @property
    def data(self):
        return fits.getdata(self.data_file.full_path, ignore_missing_end=True)


    @property
    def shape(self):
        return self.header['naxis1'], self.header['naxis2']


    def __repr__(self):
        return "<FITS file ID {0:d} @ {1}>".format(self.id,
                                                   self.data_file.full_path)