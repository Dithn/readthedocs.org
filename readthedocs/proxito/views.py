"""Views for doc serving."""

import logging
import mimetypes
import os
from functools import wraps
from urllib.parse import urlparse

from django.conf import settings
from django.core.files.storage import get_storage_class
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils.encoding import iri_to_uri
from django.views.static import serve

from readthedocs.core.resolver import resolve
from readthedocs.projects.models import Project, ProjectRelationship


log = logging.getLogger(__name__)  # noqa


def _serve_401(request, project):
    res = render(request, '401.html')
    res.status_code = 401
    log.debug('Unauthorized access to %s documentation', project.slug)
    return res


def _fallback():
    # TODO: This currently isn't used. It might be though, so keeping it for now.
    res = HttpResponse('Internal fallback to RTD app')
    res.status_code = 420
    log.debug('Falling back to RTD app')
    return res


def map_subproject_slug(view_func):
    """
    A decorator that maps a ``subproject_slug`` URL param into a Project.

    :raises: Http404 if the Project doesn't exist

    .. warning:: Does not take into account any kind of privacy settings.
    """

    @wraps(view_func)
    def inner_view(  # noqa
            request, subproject=None, subproject_slug=None, *args, **kwargs
    ):
        if subproject is None and subproject_slug:
            # Try to fetch by subproject alias first, otherwise we might end up
            # redirected to an unrelated project.
            # Depends on a project passed into kwargs
            rel = ProjectRelationship.objects.filter(
                parent=kwargs['project'],
                alias=subproject_slug,
            ).first()
            if rel:
                subproject = rel.child
            else:
                rel = ProjectRelationship.objects.filter(
                    parent=kwargs['project'],
                    child__slug=subproject_slug,
                ).first()
                if rel:
                    subproject = rel.child
                else:
                    log.warning(
                        'The slug is not subproject of project. subproject_slug=%s project_slug=%s',
                        subproject_slug, kwargs['project'].slug
                    )
                    raise Http404('Invalid subproject slug')
        return view_func(request, subproject=subproject, *args, **kwargs)

    return inner_view


def map_project_slug(view_func):
    """
    A decorator that maps a ``project_slug`` URL param into a Project.

    :raises: Http404 if the Project doesn't exist

    .. warning:: Does not take into account any kind of privacy settings.
    """

    @wraps(view_func)
    def inner_view(  # noqa
            request, project=None, project_slug=None, *args, **kwargs
    ):
        if project is None:
            # Get a slug from the request if it can't be found in the URL
            if not project_slug:
                project_slug = request.host_project_slug
                log.debug(
                    'Inserting project slug from request slug=[%s]',
                    project_slug
                )
            try:
                project = Project.objects.get(slug=project_slug)
            except Project.DoesNotExist:
                raise Http404('Project does not exist.')
        return view_func(request, project=project, *args, **kwargs)

    return inner_view


@map_project_slug
@map_subproject_slug
def redirect_page_with_filename(request, project, subproject, filename):  # pylint: disable=unused-argument  # noqa
    """Redirect /page/file.html to /<default-lang>/<default-version>/file.html."""

    urlparse_result = urlparse(request.get_full_path())
    return HttpResponseRedirect(
        resolve(
            subproject or project,
            filename=filename,
            query_params=urlparse_result.query,
        )
    )


@map_project_slug
@map_subproject_slug
def redirect_project_slug(request, project, subproject):  # pylint: disable=unused-argument
    """Handle / -> /en/latest/ directs on subdomains."""
    urlparse_result = urlparse(request.get_full_path())

    return HttpResponseRedirect(
        resolve(
            subproject or project,
            query_params=urlparse_result.query,
        ),
    )


@map_project_slug
@map_subproject_slug
def serve_docs(
        request,
        project,
        subproject,
        lang_slug=None,
        version_slug=None,
        filename='',
):
    """Take the incoming parsed URL's and figure out what file to serve."""

    log.debug(
        'project=%s, subproject=%s, lang_slug=%s, version_slug=%s, filename=%s',
        project, subproject, lang_slug, version_slug, filename
    )

    # Take the most relevant project so far
    current_project = subproject or project

    # Handle a / redirect when we aren't a single version
    if all([lang_slug is None, version_slug is None, filename == '',
            not current_project.single_version]):
        log.info('Proxito redirect: slug=%s', current_project.slug)
        return redirect_project_slug(
            request,
            project=current_project,
            subproject=None,
        )

    if (lang_slug is None or version_slug is None) and not current_project.single_version:
        log.info('Invalid URL for project with versions. url=%s', filename)
        raise Http404('Invalid URL for project with versions')

    # Handle single-version projects that have URLs like a real project
    if current_project.single_version:
        if lang_slug and version_slug:
            filename = os.path.join(lang_slug, version_slug, filename)
            lang_slug = version_slug = None

    # Check to see if we need to serve a translation
    if not lang_slug or lang_slug == current_project.language:
        final_project = current_project
    else:
        final_project = get_object_or_404(
            current_project.translations.all(), language=lang_slug
        )

    # ``final_project`` is now the actual project we want to serve docs on,
    # accounting for:
    # * Project
    # * Subproject
    # * Translations

    # TODO: Redirects need to be refactored before we can turn them on
    # They currently do 1 request per redirect that exists for the project
    # path, http_status = final_project.redirects.get_redirect_path_with_status(
    #     language=lang_slug, version_slug=version_slug, path=filename
    # )

    # Handle single version by grabbing the default version
    if final_project.single_version:
        version_slug = final_project.get_default_version()

    from readthedocs.builds.models import Version
    version = (
        Version.objects.for_project(final_project).filter(slug=version_slug)
    ).first()

    from readthedocsinc.acl.constants import VIEW_DOCS_PERMISSION

    # Check authorization of user for a version.
    # This method needs the `request` object in `user._request`.
    request.user._request = request
    if not request.user.has_perm(VIEW_DOCS_PERMISSION, version):
        if not request.user.is_authenticated:
            # Redirect to the CAS login page
            from cas.views import login as cas_login

            # HACK: since we are hitting the web server from inside the Python
            # code, we need to hit ``web`` host (because ``proxito`` host does
            # not known dev.readthedocs.io). On the other hand, when the user's
            # browser is requesting the CAS server login page, we need to
            # generate the URL for the final user using the host
            # dev.readthedocs.io
            if 'ticket=' in request.build_absolute_uri():
                settings.CAS_SERVER_URL = 'http://web:8000/cas/'
                response = cas_login(
                    request, next_page=request.build_absolute_uri()
                )
            else:
                settings.CAS_SERVER_URL = 'http://dev.readthedocs.io/cas/'
                response = cas_login(
                    request, next_page=request.build_absolute_uri()
                )

            return response


    storage_path = final_project.get_storage_path(
        type_='html', version_slug=version_slug, include_file=False
    )
    path = f'{storage_path}/{filename}'

    # Handle out backend storage not supporting directory indexes,
    # so we need to append index.html when appropriate.
    # TODO: We don't currently support `docs.example.com/en/latest/install`
    # where the user actually wants `docs.example.com/en/latest/install/index.html`
    # We would need to emulate nginx's try_files in order to handle this.
    if path[-1] == '/':
        path += 'index.html'

    # Serve from the filesystem if using PYTHON_MEDIA
    # We definitely shouldn't do this in production,
    # but I don't want to force a check for DEBUG.
    if settings.PYTHON_MEDIA:
        log.info('[Django serve] path=%s, project=%s', path, final_project)
        storage = get_storage_class(settings.RTD_BUILD_MEDIA_STORAGE)()
        root_path = storage.path('')
        # Serve from Python
        return serve(request, path, root_path)


    # NOTE: this is needed to use a PRIVATE S3 bucket. The method ``.url``
    # generates the proper ``AccessKeyId`` and ``Signature`` query arguments.
    storage = get_storage_class(settings.RTD_BUILD_MEDIA_STORAGE)()
    # We don't need the full URL here: remove the scheme and domain and only
    # keep the path and query
    url = storage.url(path)
    path = f'{urlparse(url).path}?{urlparse(url).query}'
    # Remove ``/`` to avoid MethodNotSupported
    path = path.strip('/')



    # Remove ``/media/static/`` since it will be appended automatically by the
    # storage when it tries to read the file
    # path = path.strip('/media/static/')


    # Serve via nginx
    log.info('[Nginx serve] path=%s, project=%s', path, final_project)
    return _serve_docs_nginx(
        request, final_project=final_project, path=f'/proxito/{path}'
    )


def _serve_docs_nginx(request, final_project, path):

    # Serve from Nginx
    content_type, encoding = mimetypes.guess_type(path)
    content_type = content_type or 'application/octet-stream'
    response = HttpResponse(
        f'Serving internal path: {path}', content_type=content_type
    )
    if encoding:
        response['Content-Encoding'] = encoding

    # NGINX does not support non-ASCII characters in the header, so we
    # convert the IRI path to URI so it's compatible with what NGINX expects
    # as the header value.
    # https://github.com/benoitc/gunicorn/issues/1448
    # https://docs.djangoproject.com/en/1.11/ref/unicode/#uri-and-iri-handling
    x_accel_redirect = iri_to_uri(path)
    response['X-Accel-Redirect'] = x_accel_redirect

    return response
