
import os
import yaml

import geminiutil

from geminiutil.base.alchemy.base import Base  # , Object

from geminiutil.base.alchemy.gemini_alchemy import Instrument, \
    ObservationType, ObservationClass, ObservationBlock

from geminiutil.base.alchemy.file_alchemy import (
    FITSFile, AbstractFileTable, AbstractCalibrationFileTable)

from .. import base

from geminiutil.base.alchemy import gemini_alchemy

from scipy import interpolate
from scipy.ndimage import convolve1d
from sqlalchemy import Column, ForeignKey
# from sqlalchemy import event

from sqlalchemy.orm import relationship, backref, object_session
from sqlalchemy import func, event

#sqlalchemy types
from sqlalchemy import String, Integer, Float  # DateTime, Boolean
from geminiutil.base.alchemy.point_sources import PointSource

from geminiutil.gmos.util import wavecal
from geminiutil.gmos.util.extraction import extract_spectrum, extract_arc
from geminiutil.gmos.util.estimate_disp import wavecal as estimate_disp_wavecal
from geminiutil.gmos.alchemy.base import (
    AbstractGMOSRawFITS, GMOSDatabaseDuplicate, GMOSNotPreparedError)
from geminiutil.gmos.alchemy.mos import MOSPointSource, MOSSpectrum

from astropy.utils import misc
from astropy import units as u

from astropy.table import Table

from numpy.testing import assert_almost_equal
import numpy as np

import logging


logger = logging.getLogger(__name__)


gmos_calib_dir = os.path.join(geminiutil.__path__[0], 'data', 'gmos')

detector_yaml_fname = os.path.join(gmos_calib_dir, 'gmos_detector_information.yml')
detector_information = yaml.load(file(detector_yaml_fname))

grating_eq, tilt = np.loadtxt(os.path.join(gmos_calib_dir, 'gratingeq.dat'), unpack=True)


# in the simplest case this is m*lambda = (1/ruling_density) * (sin(beta) + sin(alpha))
grating_eq_sorting = np.argsort(grating_eq)
grating_equation_interpolator = interpolate.interp1d(grating_eq[grating_eq_sorting], tilt[grating_eq_sorting])
import numpy as np


from geminiutil.gmos.gmos_prepare import GMOSPrepareFrame
from geminiutil.gmos.util.prepare_slices import fit_slice_y_distortion, \
    calculate_slice_geometries_from_mdf
from geminiutil.gmos.util.extraction import extract_spectrum

class GMOSDatabaseDuplicate(Exception):
    pass

class GMOSNotPreparedError(Exception):
    #raised if a prepared fits is needed but none found for certain tasks
    pass


class GMOSDetector(Base):
    __tablename__ = 'gmos_detector'

    """
    Detector properties table
    """

    id = Column(Integer, primary_key=True)
    naxis1 = Column(Integer)
    naxis2 = Column(Integer)
    ccd_name = Column(String)
    readout_direction = Column(String)
    gain = Column(Float)
    readout_noise = Column(Float)
    x_binning = Column(Integer)
    y_binning = Column(Integer)
    frame_id = Column(Integer)
    instrument_id = Column(Integer, ForeignKey('instrument.id'))

    instrument = relationship(Instrument)

    @classmethod
    def from_fits_object(cls, fits_object, ccd_no):
        hdu = fits_object.fits_data[ccd_no]

        header = hdu.header
        session = object_session(fits_object)

        x_binning, y_binning = map(int, header['CCDSUM'].split())

        readout_direction = header['ampname'].split(',')[1].strip()

        instrument_id = Instrument.from_fits_object(fits_object).id

        detector_object = session.query(cls).filter(cls.naxis1==header['NAXIS1'], cls.naxis2==header['NAXIS2'],
                                  cls.ccd_name==header['CCDNAME'], cls.readout_direction==readout_direction,
                                  (func.abs(cls.gain - header['GAIN']) / header['GAIN']) < 0.0001,
                                  (func.abs(cls.readout_noise - header['RDNOISE']) / header['RDNOISE']) < 0.0001,
                                  cls.x_binning==x_binning, cls.y_binning==y_binning,
                                  cls.frame_id==int(header['FRAMEID']),
                                  cls.instrument_id==instrument_id).all()
        if detector_object == []:
            detector_object = cls(header['NAXIS1'], header['NAXIS2'], header['CCDNAME'], readout_direction, header['GAIN'],
                       header['RDNOISE'], x_binning, y_binning, header['FRAMEID'], instrument_id)
            session.add(detector_object)
            session.commit()
            return detector_object
        elif len(detector_object) == 1:
            return detector_object[0]
        else:
            raise ValueError('Found more than one detectors')

    def __init__(self, naxis1, naxis2, ccd_name, readout_direction, gain, read_noise, x_binning, y_binning, frame_id, instrument_id):
        self.naxis1 = naxis1
        self.naxis2 = naxis2
        self.ccd_name = ccd_name
        self.readout_direction = readout_direction
        self.gain = gain
        self.readout_noise = read_noise
        self.x_binning = x_binning
        self.y_binning = y_binning
        self.frame_id = frame_id
        self.instrument_id = instrument_id


    @misc.lazyproperty
    def pixel_scale(self):
        if self.ccd_name.startswith('EEV'):
            return detector_information['pixel_scale'][self.instrument.name]['eev'] * u.Unit('arcsec/pixel')
        else:
            raise NotImplemented('CCD %s not implemented yet' % self.ccd_name)

    @misc.lazyproperty
    def spectral_cutoff(self):
        if self.ccd_name.startswith('EEV'):
            return detector_information['spectral_cutoff']['eev'] * u.Unit('nm')
        else:
            raise NotImplemented('CCD %s not implemented yet' % self.ccd_name)

    def __repr__(self):
        return "<detector id=%d ccdname=%s xbin=%d ybin=%d gain=%.2f>" % (self.id, self.ccd_name, self.x_binning,
        self.y_binning, self.gain)


from geminiutil.gmos.alchemy.base import GMOSGrating, GMOSFilter




class GMOSMOSInstrumentSetup2Detector(Base):
    __tablename__ = 'gmos_mos_instrument_setup2gmosdetector'

    id = Column(Integer, primary_key=True)
    instrument_setup_id = Column(Integer, ForeignKey('gmos_mos_instrument_setup.id'))
    detector_id = Column(Integer, ForeignKey('gmos_detector.id'))
    fits_extension_id = Column(Integer)



class GMOSMOSInstrumentSetup(Base):
    __tablename__ = 'gmos_mos_instrument_setup'

    id = Column(Integer, primary_key=True)

    filter1_id = Column(Integer, ForeignKey('gmos_filters.id'))
    filter2_id = Column(Integer, ForeignKey('gmos_filters.id'))

    grating_id = Column(Integer, ForeignKey('gmos_gratings.id'))

    grating_slit_wavelength_value = Column(Float)
    grating_slit_wavelength_unit = u.Unit('nm')


    @property
    def grating_slit_wavelength(self):
        return self.grating_slit_wavelength_value *\
               self.grating_slit_wavelength_unit

    grating_central_wavelength_value = Column(Float)
    grating_central_wavelength_unit = u.Unit('nm')

    @property
    def grating_central_wavelength(self):
        return self.grating_central_wavelength_value *\
               self.grating_central_wavelength_unit

    grating_tilt_value = Column(Float)
    grating_tilt_unit = u.Unit('degree')

    grating_order = Column(Integer)

    instrument_id = Column(Integer, ForeignKey('instrument.id'))

    #instrument_setup2detector_id = Column(Integer, ForeignKey('gmos_mos_instrument_setup2gmosdetector.id'))

    filter1 = relationship(GMOSFilter, primaryjoin=(GMOSFilter.id==filter1_id),
                                uselist=False)

    filter2 = relationship(GMOSFilter, primaryjoin=(GMOSFilter.id==filter2_id),
                                uselist=False)

    grating = relationship(GMOSGrating)

    instrument = relationship(Instrument)


    @classmethod
    def from_fits_object(cls, fits_object, equivalency_threshold = 0.0001):
        session = object_session(fits_object)
        header = fits_object.fits_data[0].header
        filter1 = GMOSFilter.from_keyword(header['filter1'], session)
        filter2 = GMOSFilter.from_keyword(header['filter2'], session)

        grating = GMOSGrating.from_keyword(header['grating'], session)


        grating_central_wavelength = header['centwave']
        grating_slit_wavelength = header['grwlen']

        grating_tilt = header['grtilt']
        grating_order = header['grorder']

        instrument = Instrument.from_fits_object(fits_object)

        instrument_setup_object = session.query(cls).filter(
            cls.filter1_id==filter1.id, cls.filter2_id==filter2.id,
            cls.grating_id==grating.id, cls.instrument_id==instrument.id,
            (func.abs(cls.grating_central_wavelength_value - grating_central_wavelength)
                                                    / grating_central_wavelength) < equivalency_threshold,
            (func.abs(cls.grating_slit_wavelength_value - grating_slit_wavelength)
                                                    / grating_slit_wavelength) < equivalency_threshold,
            (func.abs(cls.grating_tilt_value - grating_tilt)
                                                    / grating_tilt) < equivalency_threshold).all()


        if instrument_setup_object == []:

            instrument_setup_object = cls(filter1.id, filter2.id, grating.id, grating_central_wavelength,
                                          grating_slit_wavelength, grating_tilt, grating_order, instrument.id)

            session.add(instrument_setup_object)
            session.commit()

            for fits_extension_id in xrange(1, len(fits_object.fits_data)):
                current_detector_id = GMOSDetector.from_fits_object(fits_object, fits_extension_id).id
                session.add(GMOSMOSInstrumentSetup2Detector(instrument_setup_id=instrument_setup_object.id,
                                                            fits_extension_id=fits_extension_id,
                                                            detector_id=current_detector_id))
            session.commit()



            return instrument_setup_object

        elif len(instrument_setup_object) == 1:
            return instrument_setup_object[0]

        else:
            raise ValueError('More than one Instrument setup with the same setup found: %s' % instrument_setup_object)


    @property
    def detectors(self):
        session = object_session(self)
        detectors = session.query(GMOSDetector).join(GMOSMOSInstrumentSetup2Detector).\
            filter(GMOSMOSInstrumentSetup2Detector.instrument_setup_id==self.id).all()
        return detectors

    def __init__(self, filter1_id, filter2_id, grating_id, grating_central_wavelength_value,
                 grating_slit_wavelength_value, grating_tilt_value,
                 grating_order, instrument_id):
        self.filter1_id = filter1_id
        self.filter2_id = filter2_id
        self.grating_id = grating_id
        self.grating_central_wavelength_value = grating_central_wavelength_value
        self.grating_slit_wavelength_value = grating_slit_wavelength_value
        self.grating_tilt_value = grating_tilt_value
        self.grating_order = grating_order
        self.instrument_id = instrument_id

    def __getattr__(self, item):
        if item in ['grating_slit_wavelength', 'grating_central_wavelength', 'grating_tilt']:
            item_value = getattr(self, '%s_value' % item)
            item_unit = getattr(self, '%s_unit' % item)
            return u.Quantity(item_value, item_unit)
        else:
            return self.__getattribute__(item)

    @misc.lazyproperty
    def x_binning(self):
        x_binnings = np.array([detector.x_binning for detector in self.detectors])
        assert np.all(x_binnings == x_binnings[0])
        return x_binnings[0]

    @misc.lazyproperty
    def y_binning(self):
        y_binnings = np.array([detector.y_binning for detector in self.detectors])
        assert np.all(y_binnings == y_binnings[0])
        return y_binnings[0]

    @misc.lazyproperty
    def anamorphic_factor(self):
        return np.sin((self.calculated_grating_tilt + 50 * u.degree).to('rad').value) / \
               np.sin(self.calculated_grating_tilt.to('rad').value)


    @misc.lazyproperty
    def grating_equation_coefficient(self):
        return (self.grating.ruling_density * self.grating_central_wavelength).to(1).value


    @misc.lazyproperty
    def calculated_grating_tilt(self):
        return grating_equation_interpolator(self.grating_equation_coefficient) * u.degree


    @misc.lazyproperty
    def wavelength_start(self):
        wavelength_start_value = np.max([item.to('nm').value for item in [self.filter1.wavelength_start,
                                                                          self.filter2.wavelength_start,
                                                                          self.grating.wavelength_start]])
        return wavelength_start_value * u.Unit('nm')

    @misc.lazyproperty
    def wavelength_end(self):
        wavelength_end_value = np.min([item.to('nm').value for item in [self.filter1.wavelength_end,
                                                                        self.filter2.wavelength_end,
                                                                        self.detectors[-1].spectral_cutoff,
                                                                        self.grating.wavelength_end]])
        return wavelength_end_value * u.Unit('nm')

    @misc.lazyproperty
    def y_offset(self):
        return self.grating.y_offset / self.y_binning

    @misc.lazyproperty
    def y_distortion_coefficients(self):
        return detector_information['y-distortion'][self.instrument.name]

    @misc.lazyproperty
    def arcsec_per_mm(self):
        return detector_information['arcsecpermm'] * u.Unit('arcsec/mm')

    @misc.lazyproperty
    def x_pix_per_mm(self):
        return self.arcsec_per_mm / self.x_scale

    @misc.lazyproperty
    def y_pix_per_mm(self):
        return self.arcsec_per_mm / self.y_scale



    @misc.lazyproperty
    def chip_gap(self):
        if self.x_binning == 1:
            return detector_information['chip_gap']['unbinned']
        elif self.x_binning > 1:
            return np.int(np.round(detector_information['chip_gap']['binned'] / self.x_binning))

    def calculate_resolution(self, slit_width):
        """
            Calculate resolution

            Parameters
            ----------

            slit_width : `~astropy.u.Quantity`
                angle
        """
        return self.grating_equation_coefficient / (slit_width.to('rad').value * 81.0 *
                                             np.sin(self.calculated_grating_tilt.to('rad').value))

    @misc.lazyproperty
    def x_scale(self):
        return self.detectors[1].pixel_scale * self.x_binning

    @misc.lazyproperty
    def y_scale(self):
        return self.detectors[1].pixel_scale * self.y_binning


    @misc.lazyproperty
    def spectral_pixel_scale(self):
        xscale = self.x_scale.to('rad/pix').value
        spectral_pixel_scale_value = self.anamorphic_factor * xscale * self.grating_central_wavelength.to('nm').value * \
            81.0 * np.sin(self.calculated_grating_tilt.to('rad').value) / self.grating_equation_coefficient
        return spectral_pixel_scale_value * u.Unit('nm/pix')

    def __repr__(self):
        return "<GMOS MOS Instrument Setup ID=%s Filter1 %s Filter2 %s Grating %s Tilt %.2f central wave=%.2f %s>" % \
                (self.id, self.filter1, self.filter2, self.grating, self.grating_tilt_value, self.grating_central_wavelength_value,
                self.grating_central_wavelength_unit)

class GMOSMOSRawFITS(AbstractGMOSRawFITS):
    __tablename__ = 'gmos_mos_raw_fits'

    def prepare_to_database(self, prepare_function=None, destination_dir='.', force=False):

        session = object_session(self)

        if self.prepared is not None:
            if not force:
                raise GMOSDatabaseDuplicate('This fits has already been prepared {0}. '
                                            'Use force=True to override'.format(self.prepared))
            else:
                session.delete(self.prepared)
                session.commit()

        prepared_fits = self.prepare(prepare_function=prepare_function)
        prepared_fname = (prepare_function.file_prefix if prepare_function
                          else GMOSPrepareFrame.file_prefix)
        prepared_fname = '{0}-{1}'.format(prepared_fname, self.fits.fname)

        prepared_full_path = os.path.join(destination_dir, prepared_fname)

        prepared_fits.writeto(prepared_full_path, clobber=True)

        # read it back in and add to database
        fits_file = FITSFile.from_fits_file(prepared_full_path)


        session.add(fits_file)
        session.commit()

        gmos_mos_prepared = GMOSMOSPrepared(id=fits_file.id,
                                            raw_fits_id=self.id)
        session.add(gmos_mos_prepared)
        session.commit()

        return gmos_mos_prepared

    def prepare(self, prepare_function=None):
        if prepare_function is None:
            logger.debug('no prepare function given - using defaults')
            prepare_function = GMOSPrepareFrame()

        prepared_fits = prepare_function(self)
        return prepared_fits


class GMOSMOSPrepared(Base):
    __tablename__ = 'gmos_mos_prepared'

    id = Column(Integer, ForeignKey('fits_file.id'), primary_key=True)
    raw_fits_id = Column(Integer, ForeignKey('gmos_mos_raw_fits.id'))

    raw_fits = relationship(GMOSMOSRawFITS, backref=backref('prepared', uselist=False), uselist=False)
    fits = relationship(FITSFile, uselist=False, cascade='delete')
    #prepare_param_id = Column(Integer)

    def __repr__(self):
        return "<Prepared version of raw fits {0}>".format(self.raw_fits)



class GMOSMOSScienceSet(Base):
    __tablename__ = 'gmos_mos_science_set'

    id = Column(Integer, ForeignKey('gmos_mos_raw_fits.id'), primary_key=True)
    flat_id = Column(Integer, ForeignKey('gmos_mos_raw_fits.id'))
    mask_arc_id = Column(Integer, ForeignKey('gmos_mos_raw_fits.id'))
    longslit_arc_id = Column(Integer, ForeignKey('gmos_mos_raw_fits.id'))

    science = relationship(GMOSMOSRawFITS, primaryjoin=(GMOSMOSRawFITS.id==id),
                            backref=backref('science_set', uselist=False))
    flat = relationship(GMOSMOSRawFITS, primaryjoin=(GMOSMOSRawFITS.id==flat_id),
                        backref=backref('flat2science', uselist=False))
    mask_arc = relationship(GMOSMOSRawFITS, primaryjoin=(GMOSMOSRawFITS.id==mask_arc_id),
                            backref=backref('mask_arc2science', uselist=False))

    longslit_arc = relationship(GMOSMOSRawFITS, primaryjoin=(GMOSMOSRawFITS.id==longslit_arc_id),
                            backref=backref('long_arc2science', uselist=False))




    def calculate_slice_geometries(self, method='Nelder-Mead',
                                   slice_model_chip=2,
                                   slice_model_slice=slice(None),
                                   degree=5):

        mdf_slice_edges = calculate_slice_geometries_from_mdf(self.science)
        return fit_slice_y_distortion(self.flat, mdf_slice_edges, method=method,
                                      slice_model_chip=slice_model_chip,
                                      slice_model_slice=slice_model_slice,
                                      degree=degree)



    def calculate_slice_geometries_to_database(self, method='Nelder-Mead',
                                   slice_model_chip=2,
                                   slice_model_slice=slice(None),
                                   degree=5, point_source_priorities=[1,3], force=False):

        session = object_session(self)
        if (session.query(GMOSMOSSlice)
            .filter_by(slice_set_id=self.id).count()) > 0:
            if not force:
                raise GMOSDatabaseDuplicate('A slice set has already been created for this Science Set. To recalculate'
                                        ' and delete the existing slice set use force=True')
            else:
                logger.warn('Deleting existing slice set and recreating new one')
                session.query(GMOSMOSSlice).filter_by(slice_set_id=self.id).delete()

        mdf_table = self.science.mask.table
        slice_edges = self.calculate_slice_geometries(method=method,
                                      slice_model_chip=slice_model_chip,
                                      slice_model_slice=slice_model_slice,
                                      degree=degree)

        slices = []
        for i, line in enumerate(mdf_table):
            priority = int(line['priority'])
            source_id = int(line['ID'])

            current_slice = GMOSMOSSlice(list_id=i, slice_set_id=self.id,
                                         lower_edge=slice_edges[0][i],
                                         upper_edge=slice_edges[1][i])

            slices.append(current_slice)

            session.add(current_slice)
            session.commit()

            if priority in point_source_priorities:
                current_slice._add_point_source(
                    source_id, line['RA'], line['DEC'],
                    current_slice.default_trace_position, priority)

        session.commit()

        return slices

    def get_model_arc(self):
        session = object_session(self)
        daycals = {'B': {}, 'R': {}}
        daycalfits = session.query(GMOSLongSlitArc).all()
        daycalfits = [fil for fil in daycalfits
                      if fil.raw.mask.name.startswith('0.5')]
        for daycal in daycalfits:
            if daycal.wave_cal is None:
                if daycal.prepared is None:
                    daycal.raw.prepare_to_database()
                daycal.longslit_calibrate_to_database()


            daycals[daycal.raw.instrument_setup.grating.name[:1]][
                daycal.raw.instrument_setup.grating_central_wavelength_value
            ] = daycal.wave_cal.fname

        instrument_setup = self.science.instrument_setup
        grating_wavelength = instrument_setup.grating_central_wavelength_value

        # find corresponding day arc
        closest = min(daycals[instrument_setup.grating.name[:1]].keys(),
                      key=lambda x: abs(grating_wavelength-x))
        dayhdf5 = daycals[instrument_setup.grating.name[:1]][closest]
        lines = wavecal.read(dayhdf5, path='lines')
        dayarc = Table.read(dayhdf5, path='arc')
        filt = np.array([0.15,0.85,0.0,0.06,.88,0.06,0.,0.85,0.15])
        fs = convolve1d(dayarc['f'], filt, axis=0, mode='nearest')
        model_wide_slit_arc = Table([dayarc['w'].flatten(), fs.flatten()],
                                    names=['w','f'])
        model_wide_slit_arc.sort('w')
        model_wide_slit_arc.meta.update(lines.meta)
        return model_wide_slit_arc

    def extract_spectra_and_calibrate(self, priorities=[1], force=False):
        """Extract point-source spectra and wavelength-calibrate them

        Parameters
        ----------
        priorities : list
            spectra are extracted if a slice has at least one object
            with a priority in this list; default: [1]

        The extracted spectra are stored in the database and written to disk
        as <gmos_file>_slc<id>_src<id>.h5
        """
        model_arc = self.get_model_arc()
        slices = [slc for slc in self.slices
                  if any(src.priority in priorities
                         for src in slc.mos_point_sources)]
        session = object_session(self)
        for slc in slices:
            # extract all point sources from the slice
            if(len(slc.mos_spectra) == len(slc.mos_point_sources) and
               not force):
                logger.info('Slice {} already extracted; skipping. '
                            'Use force=True to override.'.format(slc))
                continue
            spectrum = slc.extract_and_calibrate_spectrum(model_arc)
            for i, src in enumerate(slc.mos_point_sources):
                # get spectra for individual point sources
                src_spec = Table([spectrum['x']] +
                                 [spectrum[col][:, i, :]
                                  for col in ('source', 'error', 'sky', 'chi2',
                                              'arc', 'wave')])
                src_spec.meta.update(spectrum.meta)
                # generate a spectrum object; this also writes the spectrum
                # table to disk as <gmos_file>_slc<id>_src<id>.h5
                data_file, spectrum_object = MOSSpectrum.from_table(
                    src_spec, slc, src)
                session.add_all([data_file, spectrum_object])

        session.commit()


class GMOSMOSSlice(Base):
    __tablename__ = 'gmos_mos_slices'

    id = Column(Integer, primary_key=True)
    list_id = Column(Integer)
    slice_set_id = Column(Integer, ForeignKey('gmos_mos_science_set.id'))
    lower_edge = Column(Float)
    upper_edge = Column(Float)

    science_set = relationship(GMOSMOSScienceSet, backref='slices')
    # mos_point_sources = relationship('MOSPointSource',
    #                                  secondary=mos_point_source2point_source,
    #                                  backref='slices')

    @property
    def edges(self):
        return (self.lower_edge, self.upper_edge)

    @property
    def prepared_science_fits_data(self):
        return self.science_set.science.prepared.fits.fits_data

    @property
    def prepared_arc_fits_data(self):
        return self.science_set.mask_arc.prepared.fits.fits_data

    @property
    def science_instrument_setup(self):
        return self.science_set.science.instrument_setup

    @property
    def default_trace_position(self):
        spec_pos_y = (self.science_set.science.mask
                      .table[self.list_id]['specpos_y'].astype(np.float64))
        return spec_pos_y / self.science_instrument_setup.y_binning

    def __repr__(self):
        return "<GMOS MOS Slice id={0} edges={1})>".format(self.id, self.edges)

    def _add_point_source(self, point_source_id, ra, dec,
                          slit_position, priority):
        """
        Adding a point source from the current mask table line

        Will create a new PointSource object with data about ra, dec
        and a new MOSPointSource with data about slit_position and priority

        If both these entries exist it will perform a sanity check

        finally it will perform a sanity check

        Parameters
        ----------
        point_source_id: int
        ra: float
        dec: float
        slit_position: float
        priority: int
        """

        session = object_session(self)

        # adding point_source if not exists
        if session.query(PointSource).filter_by(
                id=point_source_id).count() == 0:
            logger.info('New point source found (ID={0}). Adding to Database'
                        .format(point_source_id))
            current_point_source = PointSource(
                id=point_source_id, ra=ra, dec=dec)
            session.add(current_point_source)
            session.commit()

            current_mos_point_source = MOSPointSource(
                id=current_point_source.id, slit_position=slit_position,
                priority=priority)

            session.add(current_mos_point_source)
            session.commit()

        else:
            # ensure that the ra, dec entries in the database are the same as
            # for the current object
            current_point_source = (session.query(PointSource)
                                    .filter_by(id=point_source_id).one())
            assert_almost_equal(ra, current_point_source.ra)
            assert_almost_equal(dec, current_point_source.dec)

            # ensure that the priority, slit_position_entries in the database
            # are the same as for the current object
            current_mos_point_source = (session.query(MOSPointSource)
                                        .filter_by(id=point_source_id).one())
            assert current_mos_point_source.priority == priority
            assert_almost_equal(current_mos_point_source.slit_position,
                                slit_position)

        current_mos_point_source.slices.append(self)

    def get_prepared_science_data(self):
        fits_data = self.prepared_science_fits_data
        science_slice = slice(np.ceil(self.lower_edge).astype(int),
                              np.ceil(self.upper_edge).astype(int))
        science_slice_data = [fits_data[chip].data[science_slice, :]
                              for chip in xrange(1, 4)]

        return science_slice_data

    def get_prepared_arc_data(self):
        fits_data = self.prepared_arc_fits_data
        arc_slice = slice(np.ceil(self.lower_edge).astype(int),
                          np.ceil(self.upper_edge).astype(int))
        arc_slice_data = [fits_data[chip].data[arc_slice, :]
                          for chip in xrange(1, 4)]

        return arc_slice_data

    def get_read_noises(self):
        return [amp.header['RDNOISE']
                for amp in self.prepared_science_fits_data[1:]]

    def extract_spectrum(self, tracepos=None, model_errors=1,
                         ff_noise=0.03, skypol=0):
        return extract_spectrum(self, tracepos=tracepos,
                                model_errors=model_errors, ff_noise=ff_noise,
                                skypol=skypol)

    def extract_arc(self, extracted_spectrum):
        return extract_arc(self, extracted_spectrum=extracted_spectrum)

    def calibrate_arc(self, extracted_arc, model_arc):
        instrument_setup = self.science_set.science.instrument_setup
        grating_wavelength = (instrument_setup.grating_central_wavelength
                              .to(u.AA).value)
        anamorphic_factor = instrument_setup.anamorphic_factor

        parguess = model_arc.meta['par']
        refwave_arc = model_arc.meta['refwave']
        xref0 = (parguess[0] -
                 (grating_wavelength - refwave_arc) / parguess[3])
        specpos_x = self.science_set.science.mask.table[
            self.list_id]['specpos_x']
        xrefguess = xref0 - specpos_x / anamorphic_factor
        guess = np.hstack([xrefguess, parguess[1:]])
        wave, refwave, par = estimate_disp_wavecal(
            Table([extracted_arc['arc'], extracted_arc['x']],
                  names=('f', 'x')),
            model_arc, refwave_arc, guess)
        extracted_arc['wave'] = wave
        extracted_arc.meta['refwave'] = refwave
        extracted_arc.meta['par'] = par
        return extracted_arc

    def extract_and_calibrate_spectrum(self, model_arc,
                                       tracepos=None, model_errors=1,
                                       ff_noise=0.03, skypol=0):
        logger.info('Extracting and calibrating from {}'.format(self))
        spectrum = self.extract_spectrum(tracepos=tracepos,
                                         model_errors=model_errors,
                                         ff_noise=ff_noise, skypol=skypol)
        arc = self.extract_arc(spectrum)
        arc = self.calibrate_arc(arc, model_arc)
        spectrum['arc'] = arc['arc']
        spectrum['wave'] = arc['wave']
        spectrum.meta['refwave'] = arc.meta['refwave']
        spectrum.meta['par'] = arc.meta['par']
        return spectrum


class GMOSArcLamp(AbstractCalibrationFileTable):
    __tablename__ = 'gmos_arc_lamp'

    name = Column(String)

    def read_line_list(self):
        line_list = np.genfromtxt(self.full_path,
                                  dtype=[('w','f8'), ('ion','a7'),
                                         ('strength','i4')],
                                  delimiter=[9, 7, 8])

        return Table(line_list)

    def __repr__(self):
        return "<GMOS Arc Lamp {0}>".format(self.name)


class GMOSLongSlitArc(Base):
    __tablename__ = 'gmos_longslit_arc'

    id = Column(Integer, ForeignKey('gmos_mos_raw_fits.id'), primary_key=True)
    arc_lamp_id = Column(Integer, ForeignKey('gmos_arc_lamp.id'))


    arc_lamp = relationship(GMOSArcLamp, uselist=False, backref='arcs')
    raw = relationship(GMOSMOSRawFITS, primaryjoin=(GMOSMOSRawFITS.id==id))

    _line_identify_defaults = {'r': dict(min_curvature=[1.,1.,0.5],
                                         minlist1=[(3.,0.), (1.,0.), (0.26,0.)],
                                         minlist2=[(2.,0.), (1.,0.), (0.26,0.)],
                                         fitrange=(-50,50)),
                               'b': dict(min_curvature=[5.,3.,2.],
                                         minlist1=[(3.,1e3), (1.,3e2), (0.26,0.), (0.13,0.)],
                                         minlist2=[(1.,3e2), (0.26,0.), (0.26,0.), (0.2,0.), (0.13,0.)],
                                         fitrange=(-50,50))}

    def prepare_longslit_arc(self, destination_dir='.', force=False):
        prepare_function = GMOSPrepareFrame(bias_subslice=[slice(None), slice(1,11)],
                                  data_subslice=[slice(1150,1250),slice(-1)])
        self.raw.prepare_to_database(prepare_function=prepare_function, destination_dir=destination_dir, force=force)

    def longslit_calibrate(self, gmos_long_slit_arc_calibration=None, doplot=False):
        from geminiutil.gmos.util.longslit_arc import GMOSLongslitArcCalibration

        if self.prepared is None:
            raise GMOSNotPreparedError("This Longslit arc has not been prepared")

        if gmos_long_slit_arc_calibration is None:
            line_list = self.arc_lamp.read_line_list()['w'] * u.angstrom
            line_identify_defaults = self._line_identify_defaults[self.raw.instrument_setup.grating.name[0].lower()]
            gmos_long_slit_arc_calibration = GMOSLongslitArcCalibration(line_catalog=line_list,
                                                                        **line_identify_defaults)

        return gmos_long_slit_arc_calibration(self.prepared, doplot=doplot)



    def longslit_calibrate_to_database(self, gmos_long_slit_arc_calibration=None, destination_dir='.', force=False):

        session = object_session(self)

        if self.wave_cal is not None:
            if not force:
                raise GMOSDatabaseDuplicate('This arc already has a wavelength calibration {0}. '
                                            'Use force=True to override'.format(self.wave_cal))
            else:
                session.delete(self.wave_cal)
                session.commit()

        arctab, linesall, shift, fake = self.longslit_calibrate(gmos_long_slit_arc_calibration=gmos_long_slit_arc_calibration)

        wavecal_store_fname = 'arccal-{}.h5'.format(self.raw.fits.fname.replace('.fits',''))
        wavecal_store_full_path = os.path.join(destination_dir, wavecal_store_fname)
        arctab.write(wavecal_store_full_path, path='arc', overwrite=True, format='hdf5')
        linesall.write(wavecal_store_full_path, path='lines', append=True, format='hdf5')

        session = object_session(self)

        gmos_long_slit_wavecal = GMOSLongSlitArcWavelengthSolution(id = self.id, fname=wavecal_store_fname,
                                                                   path=os.path.abspath(destination_dir))

        session.add(gmos_long_slit_wavecal)
        session.commit()

        return gmos_long_slit_wavecal





    @property
    def prepared(self):
        return self.raw.prepared

    def __repr__(self):
        instrument_setup = self.raw.instrument_setup
        return '<GMOS long slit arc "{0}" Slit {1} Filter1 {2} Filter2 {3} Grating {4} Tilt {5} ' \
               'central wave={6:.2f} {7}>'.format(self.raw.fits.fname,
                                                  self.raw.mask.name,
                                               instrument_setup.filter1,
                                               instrument_setup.filter2,
                                               instrument_setup.grating,
                                               instrument_setup.grating_tilt_value,
                                               instrument_setup.grating_central_wavelength_value,
                                               instrument_setup.grating_central_wavelength_unit)




class GMOSLongSlitArcWavelengthSolution(AbstractFileTable):
    __tablename__ = 'gmos_longslit_wavelength_solution'

    id = Column(Integer, ForeignKey('gmos_longslit_arc.id'), primary_key=True)

    longslit_arc = relationship(GMOSLongSlitArc, uselist=False, backref=backref('wave_cal', uselist=False))


# standard decorator style
@event.listens_for(GMOSLongSlitArcWavelengthSolution, 'before_delete')
def receive_before_delete(mapper, connection, target):
    logger.info('Deleting WaveCal file {0}'.format(target.full_path))
    os.remove(target.full_path)




class GMOSMOSSliceWaveCal(Base):
    __tablename__ = 'gmos_mos_slice_wavecal'

    id = Column(Integer, primary_key=True)
    slice_id = Column(Integer, ForeignKey('gmos_mos_slices.id'))
    wave_cal_type_id = Column(Integer, ForeignKey('wave_cal_type.id'))
    raw_fits_id = Column(Integer, ForeignKey('gmos_mos_raw_fits.id'))
    list_id = Column(Integer)
    model = Column(String)

    slice = relationship(GMOSMOSSlice, backref='slice_wavecal')
