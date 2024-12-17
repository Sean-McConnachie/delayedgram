import os
import time
import base64
import argparse
import datetime as dt
from pathlib import Path
from typing import Optional, ClassVar

from instagrapi import Client
from dotenv import load_dotenv
from pydantic import BaseModel

CONFIG_FP = "config.json"

def next_upload_time(last_upload_time: Optional[dt.datetime], upload_time: dt.time, upload_delay: dt.timedelta) -> dt.datetime:
    next_upload = (dt.datetime
                   .now()
                   .replace(hour=upload_time.hour, minute=upload_time.minute, second=upload_time.second, microsecond=0)
                   + upload_delay)
    if last_upload_time is not None:
        potential_next_upload = last_upload_time + upload_delay
        if potential_next_upload > next_upload:
            next_upload = potential_next_upload
    return next_upload


class Config(BaseModel):
    default_upload_delta: dt.timedelta
    default_upload_time: dt.time
    check_interval: dt.timedelta
    unprocessed_dir_fp: str
    processed_dir_fp: str

    @staticmethod
    def from_fp(fp: str) -> "Config":
        with open(fp, "r") as f:
            return Config.model_validate_json(f.read())


class InstaUpload(BaseModel):
    META_FP: ClassVar[str] = "meta.json"
    IM_DIR: ClassVar[str]  = "ims"
    NEW_OFFSET: ClassVar[int]  = 1

    id: int
    meta: "InstaUploadMeta"
    images: list[str]

    @staticmethod
    def write_empty_upload(dir_fp: str, newest_upload: Optional["InstaUpload"], cfg: Config):
        upload_at = next_upload_time(
            newest_upload.meta.upload_at if newest_upload is not None else None,
            cfg.default_upload_time,
            cfg.default_upload_delta
        )
        new_id = newest_upload.id + InstaUpload.NEW_OFFSET if newest_upload is not None else 0

        meta = InstaUploadMeta(caption="", loc_lat=0., loc_long=0., upload_at=upload_at)
        os.makedirs(os.path.join(dir_fp, str(new_id), InstaUpload.IM_DIR))
        with open(os.path.join(dir_fp, str(new_id), InstaUpload.META_FP), "w") as f:
            f.write(meta.model_dump_json(indent=2))

    @staticmethod
    def load_from_dir(id: int, parent_dir: str) -> "InstaUpload":
        dir = os.path.join(parent_dir, str(id))
        meta = InstaUploadMeta.from_fp(os.path.join(dir, InstaUpload.META_FP))
        ims = list(os.listdir(os.path.join(dir, InstaUpload.IM_DIR)))
        return InstaUpload(id=id, meta=meta, images=ims)

    @staticmethod
    def load_all_from_parent_dir(dir_fp: str) -> list["InstaUpload"]:
        uploads = []
        for upload_fp in os.listdir(dir_fp):
            id = int(upload_fp)
            uploads.append(InstaUpload.load_from_dir(id, dir_fp))
        uploads.sort(key=lambda x: x.id)
        return uploads

    def validate_post(self):
        return len(self.images) > 0 and self.meta.loc_lat != 0. and self.meta.loc_long != 0.


class InstaUploadMeta(BaseModel):
    caption: str
    loc_lat: Optional[float]
    loc_long: Optional[float]
    upload_at: Optional[dt.datetime]

    @staticmethod
    def from_fp(fp: str) -> "InstaUploadMeta":
        with open(fp, "r") as f:
            return InstaUploadMeta.model_validate_json(f.read())


class InstaClient:
    cfg: Config
    client: Client

    def __init__(self, cfg: Config, username: str, password: str):
        self.cfg = cfg
        self.client = Client()
        self.client.login(username, password)

    @staticmethod
    def get_env_uname_pwd() -> tuple[str, str]:
        return os.getenv("INSTAGRAM_USERNAME"), base64.b64decode(os.getenv("INSTAGRAM_PASSWORD")).decode()

    def upload_post(self, upload: InstaUpload):
        loc = None
        if upload.meta.loc_lat is not None and upload.meta.loc_long is not None:
            loc = self.client.location_search(upload.meta.loc_lat, upload.meta.loc_long)[0]
            loc = self.client.location_complete(loc)

        paths = [Path(os.path.join(self.cfg.unprocessed_dir_fp, str(upload.id), InstaUpload.IM_DIR, im))
                 for im in upload.images]
        if len(paths) == 1:
            self.client.photo_upload(
                path=paths[0],
                caption=upload.meta.caption,
                location=loc,
            )
        else:
            self.client.album_upload(
                paths=paths,
                caption=upload.meta.caption,
                location=loc,
            )


SleepFor = dt.timedelta

def try_upload(cfg: Config) -> Optional[SleepFor]:
    unprocessed = InstaUpload.load_all_from_parent_dir(cfg.unprocessed_dir_fp)
    if len(unprocessed) == 0:
        print("No uploads to process")
        return None

    upload = unprocessed[0]
    if upload.meta.upload_at > dt.datetime.now():
        print("Upload not ready")
        return upload.meta.upload_at - dt.datetime.now() + dt.timedelta(seconds=1)

    if not upload.validate_post():
        print("Invalid post. Ensure there is at least one image and a location (or set location to null)")
        return None

    cli = InstaClient(cfg, *InstaClient.get_env_uname_pwd())
    try:
        cli.upload_post(upload)
        os.rename(
            os.path.join(cfg.unprocessed_dir_fp, str(upload.id)),
            os.path.join(cfg.processed_dir_fp, str(upload.id))
        )
    except Exception as e:
        print(f"Error uploading: {e}")
        return None

def startup() -> Config:
    load_dotenv()
    cfg = Config.from_fp(CONFIG_FP)

    if not os.path.exists(cfg.unprocessed_dir_fp):
        os.makedirs(cfg.unprocessed_dir_fp)
    if not os.path.exists(cfg.processed_dir_fp):
        os.makedirs(cfg.processed_dir_fp)

    return cfg

if __name__ == "__main__":
    cfg = startup()

    parser = argparse.ArgumentParser(description="delayedgram - Instagram uploader")
    parser.add_argument(
        "--new",
        action="store_true",
        help="Create a new directory for a new upload",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload the images in the unprocessed directory if the default upload delay has passed",
    )
    parser.add_argument(
        "--cron",
        action="store_true",
        help="Run indefinitely, checking for new uploads at the specified default time",
    )

    args = parser.parse_args()

    assert args.new or args.upload or args.cron, "No action specified"
    assert sum([args.new, args.upload, args.cron]) == 1, "Only one action can be specified"


    if args.new:
        processed = InstaUpload.load_all_from_parent_dir(cfg.processed_dir_fp)
        unprocessed = InstaUpload.load_all_from_parent_dir(cfg.unprocessed_dir_fp)
        all_posts = processed + unprocessed

        newest_upload = all_posts[-1] if len(all_posts) > 0 else None
        InstaUpload.write_empty_upload(cfg.unprocessed_dir_fp, newest_upload, cfg)
    elif args.upload:
        try_upload(cfg)
    elif args.cron:
        while True:
            sleep_for = try_upload(cfg)
            if sleep_for is None:
                sleep_for = cfg.check_interval
            else:
                sleep_for = min(sleep_for, cfg.check_interval)
            print(f"Sleeping for {sleep_for}")
            time.sleep(sleep_for.total_seconds())
