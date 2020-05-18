import json
import logging
import os
import requests
import shutil
import subprocess
import tarfile

from typing import cast, Optional, List, Dict, Any, Union

_FALLBACK_BACKEND_PATH = "/tmp"


class ImageInfo:
    store: 'ImageStore'
    virtual_size: int
    actual_size: int
    name: str
    format: str
    path: str

    def __init__(self, store: 'ImageStore', image_info: Dict[str, Any], path: str):
        self.store = store
        self.virtual_size = image_info["virtual-size"]
        self.actual_size = image_info["actual-size"]
        self.name = os.path.split(image_info["filename"])[-1]
        self.format = image_info["format"]
        self.path = path


class ImageStore:
    backend: str
    frontend: str
    qemu_img_bin: str

    def __init__(self, *, backend_dir: Optional[str] = None,
                 frontend_dir: Optional[str] = None) -> None:

        self.backend = backend_dir or self.__default_backend_dir()
        self.frontend = frontend_dir or self.__default_frontend_dir()
        self.qemu_img_bin = self.__default_qemu_img_bin()

        if not os.path.exists(self.backend):
            logging.debug("Creating missing ImageStore backend at '{}'".format(self.backend))
            os.makedirs(self.backend, exist_ok=True)

        if not os.path.exists(self.frontend):
            logging.debug("Creating missing ImageStore frontend at '{}'".format(self.frontend))
            os.makedirs(self.frontend, exist_ok=True)

    def __default_backend_dir(self) -> str:
        env_specified = os.getenv("TRANSIENT_BACKEND")
        if env_specified is not None:
            return env_specified

        user_home = os.getenv("HOME")
        default_xdg_data_home = None
        if user_home is not None:
            default_xdg_data_home = os.path.join(user_home, ".local", "share")

        xdg_data_home = os.getenv("XDG_DATA_HOME", default_xdg_data_home)
        if xdg_data_home is None:
            logging.warning("$HOME and $XDG_DATA_HOME not set. Using {}/transient as backend"
                            .format(_FALLBACK_BACKEND_PATH))
            xdg_data_home = _FALLBACK_BACKEND_PATH

        return os.path.join(xdg_data_home, "transient")

    def __default_frontend_dir(self) -> str:
        env_specified = os.getenv("TRANSIENT_FRONTEND")
        if env_specified is not None:
            return env_specified
        return self.__default_backend_dir()

    def __default_qemu_img_bin(self) -> str:
        return "qemu-img"

    def __image_info(self, path: str) -> ImageInfo:
        stdout = subprocess.check_output([self.qemu_img_bin,
                                          "info", "-U", "--output=json", path])
        return ImageInfo(self, json.loads(stdout), path)

    def __download_vagrant_info(self, image_name: str) -> Dict[str, Any]:
        url = "https://app.vagrantup.com/api/v1/box/{}".format(image_name)
        response = requests.get(url, allow_redirects=True)
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            raise RuntimeError("Unable to download vagrant image '{}' info. Maybe invalid image?"
                               .format(image_name))
        return cast(Dict[str, Any], json.loads(response.content))

    def __pathsafe_image_name(self, image_name: str) -> str:
        return image_name.replace("/", "_").replace(":", "_")

    def __vagrant_box_url(self, version: str, box_info: Dict[str, Any]) -> str:
        for version_info in box_info["versions"]:
            if version_info["version"] != version:
                continue
            for provider in version_info["providers"]:
                # TODO: we should also support 'qemu'
                if provider["name"] != "libvirt":
                    continue

                download_url = provider["download_url"]
                assert(isinstance(download_url, str))
                return download_url
        raise RuntimeError("No version '{}' available for {} with provider libvirt"
                           .format(version, box_info["tag"]))

    def __download_vagrant_image(self, image_name: str, destination: str) -> None:
        box_name, version = image_name.split(":")

        # For convenience, allow the user to specify the version with a v,
        # but that isn't how the API reports it
        if version.startswith("v"):
            version = version[1:]

        logging.info("Download vagrant image: box_name={}, version={}".format(box_name, version))

        box_info = self.__download_vagrant_info(box_name)
        logging.debug("Vagrant box info: {}".format(box_info))

        box_url = self.__vagrant_box_url(version, box_info)

        stream = requests.get(box_url, allow_redirects=True)

        box_destination = destination + ".box"
        with open(box_destination, 'wb') as f:
            for block in stream.iter_content(4 * 1024):
                f.write(block)

        # libvirt boxes _should_ just be tar.gz files with a box.img file, but some
        # images put these in subdirectories. Try to detect that.
        with tarfile.open(box_destination, "r") as tar:
            box_name = [name for name in tar.getnames() if name.endswith("box.img")][0]
            in_stream = tar.extractfile(box_name)
            out_stream = open(destination, 'wb')

            # mypy appears to have a bug in their type definitions. Just cast in_stream
            # to any to convince it that this is ok.
            shutil.copyfileobj(cast(Any, in_stream), out_stream)

        # And clean up the box
        os.remove(box_destination)

    def retrieve_image(self, image_name: str) -> ImageInfo:
        pathsafe_name = self.__pathsafe_image_name(image_name)
        destination = os.path.join(self.backend, pathsafe_name)

        if os.path.exists(destination):
            logging.info("Image '{}' already exists. Skipping download".format(image_name))
            return self.__image_info(destination)

        logging.info("Downloading image: {}".format(image_name))

        # For now, we only support vagrant images
        self.__download_vagrant_image(image_name, destination)

        logging.info("Finished downloading image: {}".format(image_name))
        return self.__image_info(destination)

    def create_vm_image(self, image_name: str, vm_name: str, num: int) -> ImageInfo:
        backing_image = self.retrieve_image(image_name)
        new_image_path = os.path.join(
            self.frontend, "{}-{}-{}".format(vm_name, num, backing_image.name))

        if os.path.exists(new_image_path):
            logging.info("VM image '{}' already exists. Skipping create.".format(new_image_path))
            return self.__image_info(new_image_path)

        logging.info("Creating VM Image '{}' from backing image '{}'".format(
            new_image_path, backing_image.path))

        subprocess.check_output([self.qemu_img_bin,
                                 "create", "-f", "qcow2",
                                 "-o", "backing_file={}".format(backing_image.path),
                                 new_image_path])

        logging.info("VM Image '{}' created".format(new_image_path))
        return self.__image_info(new_image_path)

    def destroy_image(self, image: Union[str, ImageInfo]) -> None:
        if isinstance(image, str):
            os.remove(image)
        else:
            os.remove(image.path)
