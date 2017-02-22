from __future__ import absolute_import, print_function

import tempfile
import logging

from django.conf import settings

from sentry import http
from sentry.tasks.base import instrumented_task
from sentry.models import (
    Project, ProjectOption, create_files_from_macho_zip
)

logger = logging.getLogger(__name__)


def get_project_from_id(project_id):
    try:
        return Project.objects.get(id=project_id)
    except Project.DoesNotExist:
        return


def get_itunes_connect_plugin():
    from sentry.plugins import plugins
    for plugin in plugins:
        if (hasattr(plugin, 'get_task') and plugin.slug == 'itunesconnect'):
            return plugin
    return None


@instrumented_task(name='sentry.tasks.sync_dsyms_from_itunes_connect',
                   time_limit=90,
                   soft_time_limit=60)
def sync_dsyms_from_itunes_connect(**kwargs):
    options = ProjectOption.objects.filter(
        key__in=[
            'itunesconnect:enabled',
            'itunesconnect:email',
            'itunesconnect:password',
        ],
    )
    plugin = get_itunes_connect_plugin()
    for opt in options:
        project = get_project_from_id(opt.project_id)

        if not plugin.is_configured(project) or not plugin.is_enabled():
            logger.warning('Plugin %r for project %r is not configured', plugin, project)
            return

        itc = plugin.get_client(project)
        for app in itc.iter_apps():
            for build in itc.iter_app_builds(app['id']):
                fetch_dsym_url.delay(project_id=opt.project_id, app=app, build=build)
                break # TODO remove
            break # TODO remove
    return


@instrumented_task(
    name='sentry.tasks.fetch_dsym_url',
    queue='itunesconnect')
def fetch_dsym_url(project_id, app, build, **kwargs):
    p = get_project_from_id(project_id)
    plugin = get_itunes_connect_plugin()
    itc = plugin.get_client(p)
    url = itc.get_dsym_url(app['id'], build['platform'], build['version'], build['build_id'])
    import pprint
    pprint.pprint(url)
    download_dsym.delay(project_id=project_id, url=url)


@instrumented_task(
    name='sentry.tasks.download_dsym',
    queue='itunesconnect')
def download_dsym(project_id, url, **kwargs):
    p = get_project_from_id(project_id)
    import pprint
    pprint.pprint(p)

    # We bump the timeout and reset it after the download
    # itc is kind of slow
    prev_timeout = settings.SENTRY_SOURCE_FETCH_TIMEOUT
    settings.SENTRY_SOURCE_FETCH_TIMEOUT = 120
    result = http.stream_download_binary(url)
    settings.SENTRY_SOURCE_FETCH_TIMEOUT = prev_timeout

    temp = tempfile.TemporaryFile()
    try:
        temp.write(result.body)
        temp.seek(0)
        create_files_from_macho_zip(temp, project=p)
    finally:
        temp.close()
