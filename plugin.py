import json
import os

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.views.decorators.csrf import ensure_csrf_cookie
from django.utils.translation import gettext as _

from app.models import Basemap
from app.plugins import Menu, MountPoint, PluginBase
from . import api, planner


def map_css():
    try:
        with open(os.path.join(settings.BASE_DIR, "webpack-stats.json"), encoding="utf-8") as f:
            chunks = json.load(f)["chunks"]["MapView"]
        return [static("app/bundles/" + c["name"]) for c in chunks if c["name"].endswith(".css")]
    except Exception:
        return []


def asset_version():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
    try:
        return str(int(max(e.stat().st_mtime for e in os.scandir(d) if e.is_file())))
    except ValueError:
        return "0"


class Plugin(PluginBase):
    def main_menu(self):
        return [Menu(_("Mission Planner"), self.public_url(""), "fa fa-paper-plane fa-fw")]

    def app_mount_points(self):
        @login_required
        @ensure_csrf_cookie
        def missions_view(request):
            groups = api.user_missions(request.user)
            if not groups:
                return redirect(self.public_url("new/"))
            return render(request, self.template_path("missions.html"), {
                "title": _("Mission Planner"),
                "plugin_url": self.public_url(""),
                "v": asset_version(),
                "groups": groups,
            })

        def planner_view(request, mid=None):
            projects = [{"id": p.id, "name": p.name} for p in api.editable_projects(request.user)]
            return render(request, self.template_path("planner.html"), {
                "title": _("Mission Planner"),
                "map_css": map_css(),
                "plugin_url": self.public_url(""),
                "v": asset_version(),
                "mission_id": json.dumps(mid),
                "has_missions": mid is not None or bool(api.user_missions(request.user)),
                "projects": json.dumps(projects),
                "can_add_project": json.dumps(request.user.has_perm("app.add_project")),
                "drones": planner.drones_json(),
                "config": planner.config_json(),
                "rc": json.dumps(api.rc_available(request)),
                "basemaps": json.dumps(Basemap.get_cached_basemaps()),
            })

        @login_required
        @ensure_csrf_cookie
        def new_view(request):
            return planner_view(request)

        @login_required
        @ensure_csrf_cookie
        def mission_view(request, mid):
            return planner_view(request, int(mid))

        return [
            MountPoint("$", missions_view),
            MountPoint("new/$", new_view),
            MountPoint(r"(?P<mid>\d+)/$", mission_view),
        ]

    def api_mount_points(self):
        return [
            MountPoint("plan$", api.Plan.as_view()),
            MountPoint("panorama-check$", api.PanoramaCheck.as_view()),
            MountPoint("takeoff$", api.Takeoff.as_view()),
            MountPoint("import$", api.Import.as_view()),
            MountPoint("export$", api.Export.as_view()),
            MountPoint("missions$", api.Missions.as_view()),
            MountPoint(r"missions/(?P<mid>\d+)$", api.Mission.as_view()),
            MountPoint("rc/detect$", api.RcDetect.as_view()),
            MountPoint("rc/browse$", api.RcBrowse.as_view()),
            MountPoint("rc/send$", api.RcSend.as_view()),
        ]
