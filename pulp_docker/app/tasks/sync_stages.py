import asyncio
import base64
import json
import hashlib
import logging

from gettext import gettext as _
from urllib.parse import urljoin, urlparse, urlunparse

from django.db import IntegrityError
from pulpcore.plugin.models import Artifact, ProgressReport, Remote
from pulpcore.plugin.stages import DeclarativeArtifact, DeclarativeContent, Stage
from pulpcore.constants import TASK_STATES

from pulp_docker.app.models import (Manifest, MEDIA_TYPE, Blob, Tag,
                                    BlobManifest, ManifestListManifest)


log = logging.getLogger(__name__)


V2_ACCEPT_HEADERS = {
    'Accept': ','.join([MEDIA_TYPE.MANIFEST_V2, MEDIA_TYPE.MANIFEST_LIST])
}


class DockerFirstStage(Stage):
    """
    The first stage of a pulp_docker sync pipeline.

    In this stage all the content is discovered, including the nested one.

    """

    def __init__(self, remote):
        """Initialize the stage."""
        super().__init__()
        self.remote = remote
        self.deferred_download = (self.remote.policy != Remote.IMMEDIATE)

    async def run(self):
        """
        DockerFirstStage.
        """
        future_manifests = []
        tag_list = []
        to_download = []
        man_dcs = {}
        total_blobs = []

        with ProgressReport(
            message='Downloading tag list', code='downloading.tag_list', total=1
        ) as pb:
            repo_name = self.remote.namespaced_upstream_name
            relative_url = '/v2/{name}/tags/list'.format(name=repo_name)
            tag_list_url = urljoin(self.remote.url, relative_url)
            list_downloader = self.remote.get_downloader(url=tag_list_url)
            await list_downloader.run(extra_data={'repo_name': repo_name})

            with open(list_downloader.path) as tags_raw:
                tags_dict = json.loads(tags_raw.read())
                tag_list = tags_dict['tags']

            # check for the presence of the pagination link header
            link = list_downloader.response_headers.get('Link')
            await self.handle_pagination(link, repo_name, tag_list)
            whitelist_tags = self.remote.whitelist_tags
            if whitelist_tags:
                tag_list = list(set(tag_list) & set(whitelist_tags))
            pb.increment()

        for tag_name in tag_list:
            relative_url = '/v2/{name}/manifests/{tag}'.format(
                name=self.remote.namespaced_upstream_name,
                tag=tag_name,
            )
            url = urljoin(self.remote.url, relative_url)
            downloader = self.remote.get_downloader(url=url)
            to_download.append(downloader.run(extra_data={'headers': V2_ACCEPT_HEADERS}))

        pb_parsed_tags = ProgressReport(
            message='Processing Tags',
            code='processing.tag',
            state=TASK_STATES.RUNNING,
            total=len(tag_list)
        )

        for download_tag in asyncio.as_completed(to_download):
            tag = await download_tag
            with open(tag.path, 'rb') as content_file:
                raw_data = content_file.read()
            content_data = json.loads(raw_data)
            media_type = content_data.get('mediaType')
            tag.artifact_attributes['file'] = tag.path
            saved_artifact = Artifact(**tag.artifact_attributes)
            try:
                saved_artifact.save()
            except IntegrityError:
                del tag.artifact_attributes['file']
                saved_artifact = Artifact.objects.get(**tag.artifact_attributes)
            tag_dc = self.create_tag(saved_artifact, tag.url)

            if media_type == MEDIA_TYPE.MANIFEST_LIST:
                list_dc = self.create_tagged_manifest_list(
                    tag_dc, content_data)
                await self.put(list_dc)
                tag_dc.extra_data['man_relation'] = list_dc
                for manifest_data in content_data.get('manifests'):
                    man_dc = self.create_manifest(list_dc, manifest_data)
                    future_manifests.append(man_dc.get_or_create_future())
                    man_dcs[man_dc.content.digest] = man_dc
                    await self.put(man_dc)
            else:
                man_dc = self.create_tagged_manifest(tag_dc, content_data, raw_data)
                await self.put(man_dc)
                tag_dc.extra_data['man_relation'] = man_dc
                self.handle_blobs(man_dc, content_data, total_blobs)
            await self.put(tag_dc)
            pb_parsed_tags.increment()

        pb_parsed_tags.state = 'completed'
        pb_parsed_tags.save()

        for manifest_future in asyncio.as_completed(future_manifests):
            man = await manifest_future
            with man._artifacts.get().file.open() as content_file:
                raw = content_file.read()
            content_data = json.loads(raw)
            man_dc = man_dcs[man.digest]
            self.handle_blobs(man_dc, content_data, total_blobs)
        for blob in total_blobs:
            await self.put(blob)

    async def handle_pagination(self, link, repo_name, tag_list):
        """
        Handle registries that have pagination enabled.
        """
        while link:
            # according RFC5988 URI-reference can be relative or absolute
            _, _, path, params, query, fragm = urlparse(link.split(';')[0].strip('>, <'))
            rel_link = urlunparse(('', '', path, params, query, fragm))
            link = urljoin(self.remote.url, rel_link)
            list_downloader = self.remote.get_downloader(url=link)
            await list_downloader.run(extra_data={'repo_name': repo_name})
            with open(list_downloader.path) as tags_raw:
                tags_dict = json.loads(tags_raw.read())
                tag_list.extend(tags_dict['tags'])
            link = list_downloader.response_headers.get('Link')

    def handle_blobs(self, man, content_data, total_blobs):
        """
        Handle blobs.
        """
        for layer in (content_data.get("layers") or content_data.get("fsLayers")):
            if not self._include_layer(layer):
                continue
            blob_dc = self.create_blob(man, layer)
            blob_dc.extra_data['blob_relation'] = man
            total_blobs.append(blob_dc)
        layer = content_data.get('config', None)
        if layer:
            blob_dc = self.create_blob(man, layer)
            blob_dc.extra_data['config_relation'] = man
            total_blobs.append(blob_dc)

    def create_tag(self, saved_artifact, url):
        """
        Create `DeclarativeContent` for each tag.

        Each dc contains enough information to be dowloaded by an ArtifactDownload Stage.

        Args:
            tag_name (str): Name of each tag

        Returns:
            pulpcore.plugin.stages.DeclarativeContent: A Tag DeclarativeContent object

        """
        tag_name = url.split('/')[-1]
        relative_url = '/v2/{name}/manifests/{tag}'.format(
            name=self.remote.namespaced_upstream_name,
            tag=tag_name,
        )
        url = urljoin(self.remote.url, relative_url)
        tag = Tag(name=tag_name)
        da = DeclarativeArtifact(
            artifact=saved_artifact,
            url=url,
            relative_path=tag_name,
            remote=self.remote,
            extra_data={'headers': V2_ACCEPT_HEADERS}
        )
        tag_dc = DeclarativeContent(content=tag, d_artifacts=[da])
        return tag_dc

    def create_tagged_manifest_list(self, tag_dc, manifest_list_data):
        """
        Create a ManifestList.

        Args:
            tag_dc (pulpcore.plugin.stages.DeclarativeContent): dc for a Tag
            manifest_list_data (dict): Data about a ManifestList
        """
        digest = "sha256:{digest}".format(digest=tag_dc.d_artifacts[0].artifact.sha256)
        relative_url = '/v2/{name}/manifests/{digest}'.format(
            name=self.remote.namespaced_upstream_name,
            digest=digest,
        )
        url = urljoin(self.remote.url, relative_url)
        manifest_list = Manifest(
            digest=digest,
            schema_version=manifest_list_data['schemaVersion'],
            media_type=manifest_list_data['mediaType'],
        )
        da = DeclarativeArtifact(
            artifact=tag_dc.d_artifacts[0].artifact,
            url=url,
            relative_path=digest,
            remote=self.remote,
            extra_data={'headers': V2_ACCEPT_HEADERS}
        )
        list_dc = DeclarativeContent(content=manifest_list, d_artifacts=[da])

        return list_dc

    def create_tagged_manifest(self, tag_dc, manifest_data, raw_data):
        """
        Create an Image Manifest.

        Args:
            tag_dc (pulpcore.plugin.stages.DeclarativeContent): dc for a Tag
            manifest_data (dict): Data about a single new ImageManifest.
            raw_data: (str): The raw JSON representation of the ImageManifest.
        """
        media_type = manifest_data.get('mediaType', MEDIA_TYPE.MANIFEST_V1)
        if media_type == MEDIA_TYPE.MANIFEST_V2:
            digest = "sha256:{digest}".format(digest=tag_dc.d_artifacts[0].artifact.sha256)
        else:

            digest = self._calculate_digest(raw_data)
        manifest = Manifest(
            digest=digest,
            schema_version=manifest_data['schemaVersion'],
            media_type=media_type
        )
        relative_url = '/v2/{name}/manifests/{digest}'.format(
            name=self.remote.namespaced_upstream_name,
            digest=digest,
        )
        url = urljoin(self.remote.url, relative_url)
        da = DeclarativeArtifact(
            artifact=tag_dc.d_artifacts[0].artifact,
            url=url,
            relative_path=digest,
            remote=self.remote,
            extra_data={'headers': V2_ACCEPT_HEADERS}
        )
        man_dc = DeclarativeContent(content=manifest, d_artifacts=[da])
        return man_dc

    def create_manifest(self, list_dc, manifest_data):
        """
        Create an Image Manifest from manifest data in a ManifestList.

        Args:
            list_dc (pulpcore.plugin.stages.DeclarativeContent): dc for a ManifestList
            manifest_data (dict): Data about a single new ImageManifest.
        """
        digest = manifest_data['digest']
        relative_url = '/v2/{name}/manifests/{digest}'.format(
            name=self.remote.namespaced_upstream_name,
            digest=digest
        )
        manifest_url = urljoin(self.remote.url, relative_url)
        da = DeclarativeArtifact(
            artifact=Artifact(),
            url=manifest_url,
            relative_path=digest,
            remote=self.remote,
            extra_data={'headers': V2_ACCEPT_HEADERS}
        )
        manifest = Manifest(
            digest=manifest_data['digest'],
            schema_version=2 if manifest_data['mediaType'] == MEDIA_TYPE.MANIFEST_V2 else 1,
            media_type=manifest_data['mediaType'],
        )
        platform = {}
        p = manifest_data['platform']
        platform['architecture'] = p['architecture']
        platform['os'] = p['os']
        platform['features'] = p.get('features', '')
        platform['variant'] = p.get('variant', '')
        platform['os.version'] = p.get('os.version', '')
        platform['os.features'] = p.get('os.features', '')
        man_dc = DeclarativeContent(
            content=manifest,
            d_artifacts=[da],
            extra_data={'relation': list_dc, 'platform': platform},
            does_batch=False,
        )
        return man_dc

    def create_blob(self, man_dc, blob_data):
        """
        Create blob.

        Args:
            man_dc (pulpcore.plugin.stages.DeclarativeContent): dc for a ImageManifest
            blob_data (dict): Data about a blob

        """
        digest = blob_data.get('digest') or blob_data.get('blobSum')
        blob_artifact = Artifact(sha256=digest[len("sha256:"):])
        blob = Blob(
            digest=digest,
            media_type=blob_data.get('mediaType', MEDIA_TYPE.REGULAR_BLOB),
        )
        relative_url = '/v2/{name}/blobs/{digest}'.format(
            name=self.remote.namespaced_upstream_name,
            digest=digest,
        )
        blob_url = urljoin(self.remote.url, relative_url)
        da = DeclarativeArtifact(
            artifact=blob_artifact,
            url=blob_url,
            relative_path=digest,
            remote=self.remote,
            extra_data={'headers': V2_ACCEPT_HEADERS},
            deferred_download=self.deferred_download
        )
        blob_dc = DeclarativeContent(
            content=blob,
            d_artifacts=[da],
        )

        return blob_dc

    def _include_layer(self, layer):
        """
        Decide whether to include a layer.

        Args:
            layer (dict): Layer reference.

        Returns:
            bool: True when the layer should be included.

        """
        foreign_excluded = (not self.remote.include_foreign_layers)
        is_foreign = (layer.get('mediaType', MEDIA_TYPE.REGULAR_BLOB) == MEDIA_TYPE.FOREIGN_BLOB)
        if is_foreign and foreign_excluded:
            log.debug(_('Foreign Layer: %(d)s EXCLUDED'), dict(d=layer))
            return False
        return True

    def _calculate_digest(self, manifest):
        """
        Calculate the requested digest of the ImageManifest, given in JSON.

        Args:
            manifest (str):  The raw JSON representation of the Manifest.

        Returns:
            str: The digest of the given ImageManifest

        """
        decoded_manifest = json.loads(manifest)
        if 'signatures' in decoded_manifest:
            # This manifest contains signatures. Unfortunately, the Docker manifest digest
            # is calculated on the unsigned version of the Manifest so we need to remove the
            # signatures. To do this, we will look at the 'protected' key within the first
            # signature. This key indexes a (malformed) base64 encoded JSON dictionary that
            # tells us how many bytes of the manifest we need to keep before the signature
            # appears in the original JSON and what the original ending to the manifest was after
            # the signature block. We will strip out the bytes after this cutoff point, add back the
            # original ending, and then calculate the sha256 sum of the transformed JSON to get the
            # digest.
            protected = decoded_manifest['signatures'][0]['protected']
            # Add back the missing padding to the protected block so that it is valid base64.
            protected = self._pad_unpadded_b64(protected)
            # Now let's decode the base64 and load it as a dictionary so we can get the length
            protected = base64.b64decode(protected)
            protected = json.loads(protected)
            # This is the length of the signed portion of the Manifest, except for a trailing
            # newline and closing curly brace.
            signed_length = protected['formatLength']
            # The formatTail key indexes a base64 encoded string that represents the end of the
            # original Manifest before signatures. We will need to add this string back to the
            # trimmed Manifest to get the correct digest. We'll do this as a one liner since it is
            # a very similar process to what we've just done above to get the protected block
            # decoded.
            signed_tail = base64.b64decode(self._pad_unpadded_b64(protected['formatTail']))
            # Now we can reconstruct the original Manifest that the digest should be based on.
            manifest = manifest[:signed_length] + signed_tail

        return "sha256:{digest}".format(digest=hashlib.sha256(manifest).hexdigest())

    def _pad_unpadded_b64(self, unpadded_b64):
        """
        Fix bad padding.

        Docker has not included the required padding at the end of the base64 encoded
        'protected' block, or in some encased base64 within it. This function adds the correct
        number of ='s signs to the unpadded base64 text so that it can be decoded with Python's
        base64 library.

        Args:
            unpadded_b64 (str): The unpadded base64 text.

        Returns:
            str: The same base64 text with the appropriate number of ='s symbols.

        """
        # The Pulp team has not observed any newlines or spaces within the base64 from Docker, but
        # Docker's own code does this same operation so it seemed prudent to include it here.
        # See lines 167 to 168 here:
        # https://github.com/docker/libtrust/blob/9cbd2a1374f46905c68a4eb3694a130610adc62a/util.go
        unpadded_b64 = unpadded_b64.replace('\n', '').replace(' ', '')
        # It is illegal base64 for the remainder to be 1 when the length of the block is
        # divided by 4.
        if len(unpadded_b64) % 4 == 1:
            raise ValueError(_('Invalid base64: {t}').format(t=unpadded_b64))
        # Add back the missing padding characters, based on the length of the encoded string
        paddings = {0: '', 2: '==', 3: '='}
        return unpadded_b64 + paddings[len(unpadded_b64) % 4]


class InterrelateContent(Stage):
    """
    Stage for relating Content to other Content.
    """

    async def run(self):
        """
        Relate each item in the input queue to objects specified on the DeclarativeContent.
        """
        async for dc in self.items():

            if dc.extra_data.get('relation'):
                self.relate_manifest_to_list(dc)
            elif dc.extra_data.get('blob_relation'):
                self.relate_blob(dc)
            elif dc.extra_data.get('config_relation'):
                self.relate_config_blob(dc)
            elif dc.extra_data.get('man_relation'):
                self.relate_manifest_tag(dc)

            await self.put(dc)

    def relate_config_blob(self, dc):
        """
        Relate a Blob to a Manifest as a config layer.

        Args:
            dc (pulpcore.plugin.stages.DeclarativeContent): dc for a Blob
        """
        configured_dc = dc.extra_data.get('config_relation')
        configured_dc.content.config_blob = dc.content
        configured_dc.content.save()

    def relate_blob(self, dc):
        """
        Relate a Blob to a Manifest.

        Args:
            dc (pulpcore.plugin.stages.DeclarativeContent): dc for a Blob
        """
        related_dc = dc.extra_data.get('blob_relation')
        thru = BlobManifest(manifest=related_dc.content, manifest_blob=dc.content)
        try:
            thru.save()
        except IntegrityError:
            pass

    def relate_manifest_tag(self, dc):
        """
        Relate an ImageManifest to a Tag.

        Args:
            dc (pulpcore.plugin.stages.DeclarativeContent): dc for a Tag
        """
        related_dc = dc.extra_data.get('man_relation')
        dc.content.tagged_manifest = related_dc.content
        try:
            dc.content.save()
        except IntegrityError:
            existing_tag = Tag.objects.get(name=dc.content.name,
                                           tagged_manifest=related_dc.content)
            dc.content.delete()
            dc.content = existing_tag

    def relate_manifest_to_list(self, dc):
        """
        Relate an ImageManifest to a ManifestList.

        Args:
            dc (pulpcore.plugin.stages.DeclarativeContent): dc for a ImageManifest
        """
        related_dc = dc.extra_data.get('relation')
        platform = dc.extra_data.get('platform')
        thru = ManifestListManifest(manifest_list=dc.content, image_manifest=related_dc.content,
                                    architecture=platform['architecture'],
                                    os=platform['os'],
                                    features=platform.get('features'),
                                    variant=platform.get('variant'),
                                    os_version=platform.get('os.version'),
                                    os_features=platform.get('os.features')
                                    )

        try:
            thru.save()
        except IntegrityError:
            pass
