import json
import os
import re
import sys
from contextlib import closing

from django.http import HttpResponse
from django.utils.translation import gettext as _
from guardian.shortcuts import get_objects_for_user
from rest_framework import exceptions, permissions
from rest_framework.parsers import MultiPartParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from app.models import Project
from . import db, planner


def problem(msg, code=400):
    return Response({"detail": msg}, status=code)


def editable_projects(user):
    return get_objects_for_user(user, "app.change_project", Project.objects.filter(deleting=False), accept_global_perms=False).order_by("-created_at")


def load_mission(c, request, mid, change=False):
    m = c.execute("SELECT * FROM missions WHERE id=?", (mid,)).fetchone()
    if m is None:
        raise exceptions.NotFound()
    project = Project.objects.filter(pk=m["project_id"], deleting=False).first()
    if project is None or not request.user.has_perm("view_project", project):
        raise exceptions.NotFound()
    can_change = request.user.has_perm("change_project", project)
    if change and not can_change:
        raise exceptions.PermissionDenied(_("Only people who can edit project \"%(project)s\" can change this mission.") % {"project": project.name})
    return m, project, can_change


def mission_payload(m, project, can_change):
    return {
        "id": m["id"], "name": m["name"], "kind": m["kind"], "revision": m["revision"],
        "project": {"id": project.id, "name": project.name},
        "settings": json.loads(m["settings"]), "geometry": json.loads(m["geometry"]), "plan": json.loads(m["plan"]),
        "can_change": can_change, "created_at": m["created_at"], "updated_at": m["updated_at"],
    }


def user_missions(user):
    with closing(db.connect()) as c:
        rows = c.execute("SELECT id, project_id, name, kind, updated_at FROM missions ORDER BY updated_at DESC, id DESC").fetchall()
    projects = {p.id: p for p in Project.objects.filter(id__in=set(r["project_id"] for r in rows), deleting=False)}
    groups = {}
    order = []
    for r in rows:
        p = projects.get(r["project_id"])
        if p is None or not user.has_perm("view_project", p):
            continue
        if p.id not in groups:
            groups[p.id] = {"id": p.id, "name": p.name, "missions": []}
            order.append(p.id)
        groups[p.id]["missions"].append(dict(r))
    return [groups[i] for i in order]


def clean_name(v):
    name = str(v or "").strip()[:255]
    if not name:
        raise exceptions.ValidationError(_("Type a name for the mission."))
    return name


def clean_mission(data):
    settings = planner.clean_settings(data.get("settings"))
    geometry = data.get("geometry") if isinstance(data.get("geometry"), dict) else {}
    plan = data.get("plan")
    if not isinstance(plan, dict):
        raise exceptions.ValidationError(_("Plan the mission before saving it."))
    try:
        plan["flights"] = [dict(f, waypoints=[list(w) for w in f["waypoints"]]) for f in plan.get("flights") or []]
        planner.clean_flights(plan["flights"])
    except planner.PlanError as e:
        raise exceptions.ValidationError(str(e))
    except (KeyError, TypeError, ValueError):
        raise exceptions.ValidationError(_("Plan the mission before saving it."))
    return settings, geometry, plan


class Plan(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        try:
            return Response(planner.plan(request.data.get("settings"), request.data.get("geometry")))
        except planner.PlanError as e:
            return problem(str(e))


class PanoramaCheck(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        try:
            return Response({"problem": planner.panorama_points_problem(request.data.get("settings"), request.data.get("geometry"), request.data.get("points") or [])})
        except planner.PlanError as e:
            return problem(str(e))


def _parts(request):
    try:
        return [f["waypoints"] for f in planner.clean_flights(request.data.get("flights"))]
    except planner.PlanError as e:
        raise exceptions.ValidationError(str(e))


class Takeoff(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        s = planner.clean_settings(request.data.get("settings"))
        parts = _parts(request)
        try:
            r = planner.compute_zones(parts, s["takeoff_tol"], s["altitude"], s["same_takeoff"])
        except planner.TerrainError as e:
            return problem(_("Could not read the ground elevation. %(error)s Check the internet connection and try again.") % {"error": str(e)})
        if r is None:
            return problem(_("Plan a mission first, then show its takeoff zone."))
        if r["sampled"] == 0:
            return problem(_("No ground elevations could be read around this mission. Check the internet connection and try again."))
        if not r["zones"]:
            return problem(_("No takeoff ground was found within %(radius)d m of the first waypoint at its elevation (within %(tol)s m). Try a larger tolerance.") % {"radius": r["radius_m"], "tol": s["takeoff_tol"]})
        return Response(r)


class Import(APIView):
    permission_classes = (permissions.IsAuthenticated,)
    parser_classes = (MultiPartParser,)

    def post(self, request):
        f = request.FILES.get("file")
        if f is None:
            return problem(_("Choose a file to import."))
        if f.size > 50 * 1024 * 1024:
            return problem(_("The file is too large to import."))
        try:
            return Response(planner.import_geometry(f.name, f.read()))
        except planner.PlanError as e:
            return problem(str(e))


def _file_response(data, filename, content_type):
    r = HttpResponse(data, content_type=content_type)
    r["Content-Disposition"] = 'attachment; filename="%s"' % filename.replace('"', "")
    return r


class Export(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        try:
            flights = planner.clean_flights(request.data.get("flights"))
            name = str(request.data.get("name") or "").strip()[:120] or _("Mission")
            part = request.data.get("part")
            part = int(part) if part is not None else None
            files, base = planner.export_files(request.data.get("settings"), flights, name, part)
        except planner.PlanError as e:
            return problem(str(e))
        except (TypeError, ValueError) as e:
            return problem(str(e))
        if not files:
            return problem(_("Choose a flight to download."))
        if len(files) == 1:
            uuid = str(request.data.get("uuid") or "").strip()
            fname = uuid + ".kmz" if re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", uuid) else files[0][0]
            return _file_response(files[0][1], fname, "application/vnd.google-earth.kmz")
        return _file_response(planner.zip_files(files), base + ".zip", "application/zip")


class Missions(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        return Response(user_missions(request.user))

    def post(self, request):
        name = clean_name(request.data.get("name"))
        pid = request.data.get("project")
        if pid == "new":
            if not request.user.has_perm("app.add_project"):
                return problem(_("Your account cannot create projects. Choose an existing project."), 403)
            project = None
        else:
            project = Project.objects.filter(pk=pid, deleting=False).first() if str(pid).isdigit() else None
            if project is None or not request.user.has_perm("change_project", project):
                return problem(_("Choose a project you can edit."))
        settings, geometry, plan = clean_mission(request.data)
        if project is None:
            project = Project.objects.create(owner=request.user, name=name)
        with closing(db.connect()) as c:
            cur = c.execute("INSERT INTO missions(project_id, name, kind, settings, geometry, plan, created_by, updated_by) VALUES(?,?,?,?,?,?,?,?)",
                            (project.id, name, settings["kind"], json.dumps(settings), json.dumps(geometry), json.dumps(plan), request.user.id, request.user.id))
            m = c.execute("SELECT * FROM missions WHERE id=?", (cur.lastrowid,)).fetchone()
        return Response(mission_payload(m, project, True), status=201)


class Mission(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, mid):
        with closing(db.connect()) as c:
            m, project, can_change = load_mission(c, request, mid)
        return Response(mission_payload(m, project, can_change))

    def put(self, request, mid):
        name = clean_name(request.data.get("name"))
        settings, geometry, plan = clean_mission(request.data)
        try:
            revision = int(request.data.get("revision"))
        except (TypeError, ValueError):
            return problem(_("Reload the mission and try again."))
        with closing(db.connect()) as c:
            m, project, _can = load_mission(c, request, mid, change=True)
            cur = c.execute("UPDATE missions SET name=?, kind=?, settings=?, geometry=?, plan=?, revision=revision+1, updated_by=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND revision=?",
                            (name, settings["kind"], json.dumps(settings), json.dumps(geometry), json.dumps(plan), request.user.id, mid, revision))
            if cur.rowcount == 0:
                return Response({"detail": _("This mission changed since you opened it."), "conflict": True}, status=409)
            m = c.execute("SELECT * FROM missions WHERE id=?", (mid,)).fetchone()
        return Response(mission_payload(m, project, True))

    def delete(self, request, mid):
        with closing(db.connect()) as c:
            load_mission(c, request, mid, change=True)
            c.execute("DELETE FROM missions WHERE id=?", (mid,))
        return Response({"success": True})


LOCAL = ("127.0.0.1", "::1", "localhost")


def rc_available(request):
    if sys.platform != "win32" or os.path.exists("/.dockerenv"):
        return False
    if request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("HTTP_X_REAL_IP"):
        return False
    return request.META.get("REMOTE_ADDR") in LOCAL


def require_rc(request):
    if not rc_available(request):
        raise exceptions.PermissionDenied(_("Sending to the controller only works when the browser runs on the same Windows computer as this server."))


class RcDetect(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        require_rc(request)
        from . import rc
        r = rc.detect()
        for m in r["missions"]:
            m["waypoints"] = [[round(a, 7), round(b, 7)] for a, b in m["waypoints"]]
        return Response(r)


class RcBrowse(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        require_rc(request)
        from . import rc
        parts = [str(p) for p in (request.data.get("parts") or [])][:32]
        if request.data.get("list"):
            status, missions = rc.list_missions_at_path(parts)
            for m in missions:
                m["waypoints"] = [[round(a, 7), round(b, 7)] for a, b in m["waypoints"]]
            return Response({"status": status, "path": "\\".join(parts), "missions": missions})
        return Response({"children": rc.list_shell_children(parts)})


class RcSend(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        require_rc(request)
        from . import rc
        try:
            flights = planner.clean_flights([request.data.get("flight")])
            name = str(request.data.get("name") or "").strip()[:120] or _("Mission")
            create_ms = request.data.get("create_ms")
            create_ms = int(create_ms) if create_ms else None
            data = planner.kmz(request.data.get("settings"), flights[0], name, create_ms)
        except planner.PlanError as e:
            return problem(str(e))
        except (TypeError, ValueError) as e:
            return problem(str(e))
        path = str(request.data.get("path") or "")
        if not path:
            return problem(_("Detect the controller first."))
        ok, detail = rc.replace_controller(path, str(request.data.get("uuid") or ""), data)
        if not ok:
            return problem(detail)
        return Response({"uuid": detail, "waypoints": len(flights[0]["waypoints"])})
