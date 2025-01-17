import logging
from datetime import datetime

import numpy as np
from autoscript_sdb_microscope_client import SdbMicroscopeClient
from autoscript_sdb_microscope_client.enumerations import (
    CoordinateSystem,
    ManipulatorCoordinateSystem,
)
from autoscript_sdb_microscope_client.structures import StagePosition, Rectangle, RunAutoFocusSettings

from fibsem import acquire, movement
from fibsem.structures import (
    BeamSettings,
    MicroscopeState,
    BeamType,
    ImageSettings,
    MicroscopeSettings,
    BeamSystemSettings,
    GammaSettings
)

from pathlib import Path
import skimage
from skimage.morphology import disk
from skimage.filters.rank import gradient

def auto_link_stage(microscope: SdbMicroscopeClient, hfw: float = 150e-6) -> None:
    """Automatically focus and link sample stage z-height.

    Notes:
        - Focusing determines the working distance (focal distance) of the beam
        - Relinking is required whenever there is a significant change in vertical distance, i.e. moving
          from the landing grid to the sample grid.
        - Linking determines the specimen coordinate system, as it is defined as the relative dimensions of the top of stage
          to the instruments.
    """

    microscope.imaging.set_active_view(BeamType.ELECTRON.value)
    original_hfw = microscope.beams.electron_beam.horizontal_field_width.value
    microscope.beams.electron_beam.horizontal_field_width.value = hfw
    acquire.autocontrast(microscope, beam_type=BeamType.ELECTRON)
    microscope.auto_functions.run_auto_focus()
    microscope.specimen.stage.link()
    # NOTE: replace with auto_focus_and_link if performance of focus is poor
    # # Restore original settings
    microscope.beams.electron_beam.horizontal_field_width.value = original_hfw

def auto_focus_beam(microscope: SdbMicroscopeClient, image_settings: ImageSettings,  
    mode: str = "default",  
    wd_delta: float = 0.05e-3, steps: int  = 5, 
    reduced_area: Rectangle = Rectangle(0.3, 0.3, 0.4, 0.4), focus_image_settings: ImageSettings = None ) -> None:

    if mode == "default":
        microscope.imaging.set_active_device(BeamType.ELECTRON.value)
        microscope.imaging.set_active_view(BeamType.ELECTRON.value)  # set to Ebeam
        
        focus_settings = RunAutoFocusSettings()
        microscope.auto_functions.run_auto_focus()

    if mode == "sharpness":
        
        if focus_image_settings is None:
            focus_image_settings = ImageSettings(
                resolution = "768x512",
                dwell_time = 200e-9,
                hfw=50e-6,
                beam_type = BeamType.ELECTRON,
                save=False,
                autocontrast=True,
                gamma=GammaSettings(enabled=False),
                label=None

            )

        current_wd = microscope.beams.electron_beam.working_distance.value
        logging.info(f"sharpness (accutance) based auto-focus routine")

        logging.info(f"initial working distance: {current_wd:.2e}")

        min_wd = current_wd - (steps*wd_delta/2)
        max_wd = current_wd + (steps*wd_delta/2)

        working_distances = np.linspace(min_wd, max_wd, steps+1)

        # loop through working distances and calculate the sharpness (acutance)
        # highest acutance is best focus
        sharpeness_metric = []
        for i, wd in enumerate(working_distances):
            
            logging.info(f"Img {i}: {wd:.2e}")
            microscope.beams.electron_beam.working_distance.value = wd
            
            img = acquire.new_image(microscope, focus_image_settings, reduced_area=reduced_area)

            # sharpness (Acutance: https://en.wikipedia.org/wiki/Acutance
            out = gradient(skimage.filters.median(np.copy(img.data)), disk(5))

            sharpness = np.mean(out)
            sharpeness_metric.append(sharpness)

        # select working distance with max acutance
        idx = np.argmax(sharpeness_metric)

        pairs = list(zip(working_distances, sharpeness_metric))
        logging.info([f"{wd:.2e}: {metric:.4f}" for wd, metric in pairs])
        logging.info(f"{idx}, {working_distances[idx]:.2e}, {sharpeness_metric[idx]:.4f}")

        # reset working distance
        microscope.beams.electron_beam.working_distance.value = working_distances[idx]

        # run fine auto focus and link
        # microscope.imaging.set_active_device(BeamType.ELECTRON.value)
        # microscope.imaging.set_active_view(BeamType.ELECTRON.value)  # set to Ebeam
        # microscope.auto_functions.run_auto_focus()
        # microscope.specimen.stage.link()

    if mode == "dog":
        # TODO: implement difference of gaussian based auto-focus

        pass


    return 

def auto_charge_neutralisation(
    microscope: SdbMicroscopeClient,
    image_settings: ImageSettings,
    discharge_settings: ImageSettings = None,
    n_iterations: int = 10,
    save_images: bool = False
) -> None:

    # take sequence of images quickly,

    # get initial image
    from fibsem.utils import current_timestamp
    ts = current_timestamp()
    image_settings.label = f"{ts}_charge_neutralisation_start"
    acquire.new_image(microscope, image_settings)

    # use preset settings if not defined
    if discharge_settings is None:
        discharge_settings = ImageSettings(
            resolution = "768x512",
            dwell_time = 200e-9,
            hfw=image_settings.hfw,
            beam_type = image_settings.beam_type,
            save=False,
            save_path=image_settings.save_path,
            autocontrast=False,
            gamma=GammaSettings(enabled=False),
            label=None
        )

    for i in range(n_iterations):
        if i % 5 == 0:
            discharge_settings.save = save_images 
            discharge_settings.label = f"{ts}_charge_neutralisation_{i}"
        else:
            discharge_settings.save = False
        acquire.new_image(microscope, discharge_settings)

    # take image
    image_settings.label = f"{ts}_charge_neutralisation_end"
    acquire.new_image(microscope, image_settings)

    logging.info(f"BAM! and the charge is gone!") # important information  

def auto_needle_calibration(
    microscope: SdbMicroscopeClient, settings: MicroscopeSettings, validate: bool = True
):
    # set coordinate system
    microscope.specimen.manipulator.set_default_coordinate_system(
        ManipulatorCoordinateSystem.STAGE
    )

    # current working distance
    wd = microscope.beams.electron_beam.working_distance.value
    needle_wd_eb = 4.0e-3

    # focus on the needle
    microscope.beams.electron_beam.working_distance.value = needle_wd_eb
    microscope.specimen.stage.link()

    settings.image.hfw = 2700e-6
    acquire.take_reference_images(microscope, settings.image)

    # very low res alignment
    hfws = [2700e-6, 900e-6, 400e-6, 150e-6]
    for hfw in hfws:
        settings.image.hfw = hfw
        align_needle_to_eucentric_position(microscope, settings, validate=validate)

    # restore working distance
    microscope.beams.electron_beam.working_distance.value = wd
    microscope.specimen.stage.link()    

    logging.info(f"Finished automatic needle calibration.")


def align_needle_to_eucentric_position(
    microscope: SdbMicroscopeClient,
    settings: MicroscopeSettings,
    validate: bool = False,
) -> None:
    """Move the needle to the eucentric position, and save the updated position to disk

    Args:
        microscope (SdbMicroscopeClient): autoscript microscope instance
        settings (MicroscopeSettings): microscope settings
        validate (bool, optional): validate the alignment. Defaults to False.
    """

    from fibsem.ui import windows as fibsem_ui_windows
    from fibsem.detection.utils import FeatureType, Feature
    from fibsem.detection import detection
    
    # take reference images
    settings.image.save = False
    settings.image.beam_type = BeamType.ELECTRON

    det = fibsem_ui_windows.detect_features_v2(
        microscope=microscope,
        settings=settings,
        features=[
            Feature(FeatureType.NeedleTip, None),
            Feature(FeatureType.ImageCentre, None),
        ],
        validate=validate,
    )
    detection.move_based_on_detection(microscope, settings, det, beam_type=settings.image.beam_type)

    # take reference images
    settings.image.save = False
    settings.image.beam_type = BeamType.ION

    image = acquire.new_image(microscope, settings.image)

    det = fibsem_ui_windows.detect_features_v2(
        microscope=microscope,
        settings=settings,
        features=[
            Feature(FeatureType.NeedleTip, None),
            Feature(FeatureType.ImageCentre, None),
        ],
        validate=validate,
    )
    detection.move_based_on_detection(microscope, settings, det, beam_type=settings.image.beam_type, move_x=False)

    # take image
    acquire.take_reference_images(microscope, settings.image)

def auto_home_and_link(microscope: SdbMicroscopeClient, state: MicroscopeState = None) -> None:

    import os
    from fibsem import utils, config
    
    # home the stage
    logging.info(f"Homing stage...")
    microscope.specimen.stage.home()

    # if no state provided, use the default 
    if state is None:
        path = os.path.join(config.CONFIG_PATH, "calibrated_state.yaml")
        state = MicroscopeState.__from_dict__(utils.load_yaml(path))
    
    # move to saved linked state
    set_microscope_state(microscope, state)

    # link
    logging.info("Linking stage...")
    acquire.autocontrast(microscope, beam_type=BeamType.ELECTRON)
    microscope.auto_functions.run_auto_focus()
    microscope.specimen.stage.link()


def auto_home_and_link_v2(microscope: SdbMicroscopeClient, state: MicroscopeState = None) -> None:

    # home the stage and return the linked state
    
    if state is None:
        state = get_current_microscope_state(microscope)

    # home the stage
    logging.info(f"Homing stage...")
    microscope.specimen.stage.home()

    # move to saved linked state
    set_microscope_state(microscope, state)

    # relink (set state also links...)
    microscope.specimen.stage.link()


# STATE MANAGEMENT


def get_raw_stage_position(microscope: SdbMicroscopeClient) -> StagePosition:
    """Get the current stage position in raw coordinate system, and switch back to specimen"""
    microscope.specimen.stage.set_default_coordinate_system(CoordinateSystem.RAW)
    stage_position = microscope.specimen.stage.current_position
    microscope.specimen.stage.set_default_coordinate_system(CoordinateSystem.SPECIMEN)

    return stage_position


def get_current_microscope_state(microscope: SdbMicroscopeClient) -> MicroscopeState:
    """Get the current microscope state v2 """

    current_microscope_state = MicroscopeState(
        timestamp=datetime.timestamp(datetime.now()),
        # get absolute stage coordinates (RAW)
        absolute_position=get_raw_stage_position(microscope),
        # electron beam settings
        eb_settings=BeamSettings(
            beam_type=BeamType.ELECTRON,
            working_distance=microscope.beams.electron_beam.working_distance.value,
            beam_current=microscope.beams.electron_beam.beam_current.value,
            hfw=microscope.beams.electron_beam.horizontal_field_width.value,
            resolution=microscope.beams.electron_beam.scanning.resolution.value,
            dwell_time=microscope.beams.electron_beam.scanning.dwell_time.value,
        ),
        # ion beam settings
        ib_settings=BeamSettings(
            beam_type=BeamType.ION,
            working_distance=microscope.beams.ion_beam.working_distance.value,
            beam_current=microscope.beams.ion_beam.beam_current.value,
            hfw=microscope.beams.ion_beam.horizontal_field_width.value,
            resolution=microscope.beams.ion_beam.scanning.resolution.value,
            dwell_time=microscope.beams.ion_beam.scanning.dwell_time.value,
        ),
    )

    return current_microscope_state


def set_microscope_state(
    microscope: SdbMicroscopeClient, microscope_state: MicroscopeState
):
    """Reset the microscope state to the provided state"""

    logging.info(f"restoring microscope state...")
    
    i = 0
    while get_raw_stage_position(microscope) != microscope_state.absolute_position:

        logging.info(f"restoring stage position: {i}")
        
        # move to position
        movement.safe_absolute_stage_movement(
            microscope=microscope, stage_position=microscope_state.absolute_position
        )

        i += 1
        if i > 3:
            break

    # restore electron beam
    logging.info(f"restoring electron beam settings...")
    microscope.beams.electron_beam.working_distance.value = (
        microscope_state.eb_settings.working_distance
    )
    microscope.beams.electron_beam.beam_current.value = (
        microscope_state.eb_settings.beam_current
    )
    microscope.beams.electron_beam.horizontal_field_width.value = (
        microscope_state.eb_settings.hfw
    )
    microscope.beams.electron_beam.scanning.resolution.value = (
        microscope_state.eb_settings.resolution
    )
    microscope.beams.electron_beam.scanning.dwell_time.value = (
        microscope_state.eb_settings.dwell_time
    )
    # microscope.beams.electron_beam.stigmator.value = (
    #     microscope_state.eb_settings.stigmation
    # )


    # restore ion beam
    logging.info(f"restoring ion beam settings...")
    microscope.beams.ion_beam.working_distance.value = (
        microscope_state.ib_settings.working_distance
    )
    microscope.beams.ion_beam.beam_current.value = (
        microscope_state.ib_settings.beam_current
    )
    microscope.beams.ion_beam.horizontal_field_width.value = (
        microscope_state.ib_settings.hfw
    )
    microscope.beams.ion_beam.scanning.resolution.value = (
        microscope_state.ib_settings.resolution
    )
    microscope.beams.ion_beam.scanning.dwell_time.value = (
        microscope_state.ib_settings.dwell_time
    )
    # microscope.beams.ion_beam.stigmator.value = microscope_state.ib_settings.stigmation

    microscope.specimen.stage.link()
    logging.info(f"microscope state restored")
    return


def get_current_beam_system_state(microscope: SdbMicroscopeClient, beam_type: BeamType):

    if beam_type is BeamType.ELECTRON:
        microscope_beam = microscope.beams.electron_beam
    if beam_type is BeamType.ION:
        microscope_beam = microscope.beams.ion_beam


    # set beam active view and device
    microscope.imaging.set_active_view(beam_type.value)
    microscope.imaging.set_active_device(beam_type.value)

    # get current beam settings 
    voltage = microscope_beam.high_voltage.value
    current = microscope_beam.beam_current.value
    detector_type = microscope.detector.type.value
    detector_mode = microscope.detector.mode.value

    if beam_type is BeamType.ION:
        eucentric_height = 16.5e-3
        plasma_gas = microscope_beam.source.plasma_gas.value
    else:
        eucentric_height =  4.0e-3
        plasma_gas = None
   
    return BeamSystemSettings(
        beam_type=beam_type,
        voltage = voltage,
        current = current,
        detector_type = detector_type,
        detector_mode = detector_mode,
        eucentric_height = eucentric_height,
        plasma_gas = plasma_gas
    )

# TODO: migrate to this...from validation func
def set_beam_system_state(microscope: SdbMicroscopeClient, beam_system_settings: BeamSystemSettings):

    if beam_system_settings.beam_type is BeamType.ELECTRON:
        microscope_beam = microscope.beams.electron_beam
    if beam_system_settings.beam_type is BeamType.ION:
        microscope_beam = microscope.beams.ion_beam

    # set beam active view and device
    microscope.imaging.set_active_view(beam_system_settings.beam_type.value)
    microscope.imaging.set_active_device(beam_system_settings.beam_type.value)

    # set beam settings
    microscope_beam.high_voltage.value = beam_system_settings.voltage
    microscope_beam.beam_current.value = beam_system_settings.current
    microscope.detector.type.value = beam_system_settings.detector_type
    microscope.detector.mode.value = beam_system_settings.detector_mode

    if beam_system_settings.beam_type is BeamType.ION:
        microscope_beam.source.plasma_gas.value = beam_system_settings.plasma_gas

    return


