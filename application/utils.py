import logging
import os
from datetime import datetime
import traceback
import shutil
import zipfile
import librosa

from main import socketio
from dataset.audio_processing import convert_audio
from dataset.analysis import save_dataset_info
from dataset.clip_generator import MIN_LENGTH, MAX_LENGTH


class SocketIOHandler(logging.Handler):
    """
    Sends logger messages to the frontend using flask-socketio.
    These are handled in application.js
    """

    def emit(self, record):
        text = record.getMessage()
        if text.startswith("Progress"):
            text = text.split("-")[1]
            current, total = text.split("/")
            socketio.emit("progress", {"number": current, "total": total}, namespace="/voice")
        elif text.startswith("Status"):
            socketio.emit("status", {"text": text.replace("Status -", "")}, namespace="/voice")
        else:
            socketio.emit("logs", {"text": text}, namespace="/voice")


# Data
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice")
logger.addHandler(SocketIOHandler())
thread = None


def background_task(func, **kwargs):
    """
    Runs a background task.
    If function errors out it will send an error log to the error logging server and page.
    Sends 'done' message to frontend when complete.

    Parameters
    ----------
    func : function
        Function to run in background
    kwargs : kwargs
        Kwargs to pass to function
    """
    try:
        socketio.sleep(5)
        func(logging=logger, **kwargs)
    except Exception as e:
        error = {"type": e.__class__.__name__, "text": str(e), "stacktrace": traceback.format_exc()}
        socketio.emit("error", error, namespace="/voice")
        raise e

    socketio.emit("done", {"text": None}, namespace="/voice")


def start_progress_thread(func, **kwargs):
    """
    Starts a background task using socketio.

    Parameters
    ----------
    func : function
        Function to run in background
    kwargs : kwargs
        Kwargs to pass to function
    """
    global thread
    print("Starting Thread")
    thread = socketio.start_background_task(background_task, func=func, **kwargs)


def get_next_url(urls, path):
    """
    Returns the URL of the next step in the voice cloning process.

    Parameters
    ----------
    urls : dict
        Frontend url paths and names
    path : str
        Current URL

    Returns
    -------
    str
        URL of next step or '' if not found
    """
    urls = list(urls.keys())
    next_url_index = urls.index(path) + 1
    return urls[next_url_index] if next_url_index < len(urls) else ""


def get_suffix():
    """
    Generates a filename suffix using the currrent datetime.

    Returns
    -------
    str
        String suffix
    """
    return datetime.now().strftime("%d-%m-%Y_%H-%M-%S")


def delete_folder(path):
    """
    Deletes a folder.

    Parameters
    ----------
    path : str
        Path to folder

    Raises
    -------
    AssertionError
        If folder is not found
    """
    assert os.path.isdir(path), f"{path} does not exist"
    shutil.rmtree(path)


def import_dataset(dataset, dataset_directory, audio_folder, logging):
    """
    Imports a dataset zip into the app.
    Checks required files are present, saves the files,
    converts the audio to the required format and generates the info file.
    Deletes given zip regardless of success.

    Parameters
    ----------
    dataset : str
        Path to dataset zip
    dataset_directory : str
        Destination path for the dataset
    audio_folder : str
        Destination path for the dataset audio
    logging : logging
        Logging object to write logs to

    Raises
    -------
    AssertionError
        If files are missing or invalid
    """
    try:
        with zipfile.ZipFile(dataset, mode="r") as z:
            files_list = z.namelist()
            assert (
                "metadata.csv" in files_list
            ), "Dataset missing metadata.csv. Make sure this file is in the root of the zip file"

            folders = [x.split("/")[0] for x in files_list if "/" in x]
            assert (
                "wavs" in folders
            ), "Dataset missing wavs folder. Make sure this folder is in the root of the zip file"

            wavs = [x for x in files_list if x.startswith("wavs/") and x.endswith(".wav")]
            assert wavs, "No wavs found in wavs folder"

            metadata = z.read("metadata.csv").decode("utf-8", "ignore").replace("\r\n", "\n")
            num_metadata_rows = len([row for row in metadata.split("\n") if row])
            assert (
                len(wavs) == num_metadata_rows
            ), f"Number of wavs and labels do not match. metadata: {num_metadata_rows}, wavs: {len(wavs)}"

            logging.info("Creating directory")
            os.makedirs(dataset_directory, exist_ok=False)
            os.makedirs(audio_folder, exist_ok=False)

            # Save metadata
            logging.info("Saving files")
            with open(os.path.join(dataset_directory, "metadata.csv"), "w", encoding="utf-8") as f:
                f.write(metadata)

            # Save wavs
            total_wavs = len(wavs)
            clip_lengths = []
            filenames = {}
            for i in range(total_wavs):
                wav = wavs[i]
                data = z.read(wav)
                path = os.path.join(dataset_directory, "wavs", wav.split("/")[1])
                with open(path, "wb") as f:
                    f.write(data)
                    new_path = convert_audio(path)
                    duration = librosa.get_duration(filename=new_path)
                    assert (
                        duration >= MIN_LENGTH and duration <= MAX_LENGTH
                    ), f"{wav} is an invalid duration (must be {MIN_LENGTH}-{MAX_LENGTH}, is {duration})"
                    clip_lengths.append(duration)

                    filenames[path] = new_path
                logging.info(f"Progress - {i+1}/{total_wavs}")

            # Get around "file in use" by using delay
            logging.info("Deleting temp files")
            for old_path, new_path in filenames.items():
                os.remove(old_path)
                os.rename(new_path, old_path)

            # Create info file
            logging.info("Creating info file")
            save_dataset_info(
                os.path.join(dataset_directory, "metadata.csv"),
                os.path.join(dataset_directory, "wavs"),
                os.path.join(dataset_directory, "info.json"),
                clip_lengths=clip_lengths,
            )
    except Exception as e:
        os.remove(dataset)
        raise e

    os.remove(dataset)
