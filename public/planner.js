(function(){
    var _ = MM._, f = MM.f, esc = MM.esc;
    var $id = function(id){ return document.getElementById(id); };
    var C = MM.config, D = {};
    MM.drones.forEach(function(d){ D[d.code] = d; });
    var clone = function(o){ return JSON.parse(JSON.stringify(o)); };
    var S = clone(C.defaults);
    var G = { polygon: null, line: null, centre: null, points: [], breaks: [] };
    var mission = null, plan = null, dirty = false, ringIndex = 0, saved = {};
    var planSeq = 0, planTimer = null, busyTimer = null, tool = null;
    var FLIGHT_COLORS = ["#FFE600", "#ff9f43", "#a88bff", "#35c99a", "#ff6f91"];
    var START_COLOR = "#69B7FF", END_COLOR = "#FF8A78", MID_COLOR = "#050C14";
    var TAKEOFF_COLORS = ["#4A148C", "#7B1FA2", "#9C27B0", "#673AB7", "#AB47BC", "#3F51B5", "#BA68C8", "#9575CD"];
    var AREA_COLOR = "#7FD69C";
    var UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

    var map = MM.map("mp-map");
    var initialView = function(){
        if (!MM.missionId){
            try{
                var v = JSON.parse(localStorage.getItem("mp-map-view"));
                if (v && isFinite(v[0]) && isFinite(v[1]) && isFinite(v[2])){
                    map.setView([v[0], v[1]], v[2]);
                    return;
                }
            }catch(e){}
        }
        map.fitBounds([[46.84208471, -91.9947842], [46.84317076, -91.99307131]]);
    };
    initialView();
    map.on("moveend", function(){
        var c = map.getCenter();
        try{ localStorage.setItem("mp-map-view", JSON.stringify([c.lat, c.lng, map.getZoom()])); }catch(e){}
    });
    ["mpArea", "mpPath", "mpEdit"].forEach(function(n, i){ map.createPane(n).style.zIndex = 420 + i * 20; });
    var overlayLayer = L.layerGroup().addTo(map), areaLayer = L.layerGroup().addTo(map);
    var pathLayer = L.layerGroup().addTo(map), dotsLayer = L.layerGroup(), editLayer = L.layerGroup().addTo(map);
    var takeoffLayer = null;
    map.on("zoomend", function(){
        if (map.getZoom() >= 16) dotsLayer.addTo(map);
        else map.removeLayer(dotsLayer);
    });

    var kind = function(){ return S.kind; };
    var full = function(){ return S.kind === "panorama" || S.capture === "full"; };
    var drone = function(){ return D[S.drone]; };
    var pointKind = function(){ return S.kind === "orbit" || S.kind === "panorama"; };
    var polyKind = function(){ return S.kind === "2d" || S.kind === "oblique" || (S.kind === "panorama" && S.panorama_layout === "area"); };

    var fmtDuration = function(s){
        s = Math.max(0, Math.round(s));
        return s >= 60 ? Math.floor(s / 60) + "m " + (s % 60) + "s" : s + "s";
    };
    var fmtDistance = function(m){
        m = Math.max(0, m);
        if (m >= 1000) return (m / 1000).toFixed(m < 10000 ? 2 : 1) + " km";
        return Math.round(m) + " m";
    };
    var fmtInt = function(n){ return Number(n).toLocaleString(); };
    var round = function(v, d){ var p = Math.pow(10, d); return Math.round(v * p) / p; };

    var gsdFor = function(alt){ var d = drone(); return alt * d.sw * 100 / (d.f * d.iw); };
    var footprintAlong = function(){ var d = drone(); return S.altitude * d.sh / d.f; };

    var hasArea = function(){
        if (S.kind === "corridor") return !!G.line;
        if (S.kind === "orbit") return !!G.centre;
        if (S.kind === "panorama") return S.panorama_layout === "points" ? G.points.length > 0 : !!G.polygon;
        return !!G.polygon;
    };

    var geometryFor = function(){
        var g = { breaks: G.breaks };
        if (S.kind === "corridor") g.line = G.line;
        else if (S.kind === "orbit") g.centre = G.centre;
        else if (S.kind === "panorama" && S.panorama_layout === "points") g.points = G.points;
        else g.polygon = G.polygon;
        return g;
    };

    var show = function(id, on){ var e = $id(id); if (e) e.hidden = !on; };
    var setMsg = function(id, text, cls, retry){
        var e = $id(id);
        e.textContent = text || "";
        e.hidden = !text;
        if (cls) e.className = "alert alert-" + cls;
        if (text && retry) e.appendChild(retryLink(retry));
    };
    var retryLink = function(fn){
        var b = document.createElement("button");
        b.type = "button";
        b.className = "btn btn-link mm-retry";
        b.innerHTML = '<i class="fa fa-redo" aria-hidden="true"></i> ' + esc(_("Try again"));
        b.onclick = function(){ fn(); };
        return b;
    };

    // Tabs
    var views = ["mission", "flight", "options", "takeoff", "export"];
    var setView = function(v, focus){
        $id("mm-shell").setAttribute("data-view", v);
        views.forEach(function(x){
            var t = $id("mm-tab-" + x);
            t.setAttribute("aria-selected", x === v ? "true" : "false");
            t.tabIndex = x === v ? 0 : -1;
            t.querySelector(".mm-tab-icon").classList.toggle("theme-background-highlight", x === v);
        });
        try{ localStorage.setItem("mp-view", v); }catch(e){}
        if (focus) $id("mm-panel-" + v).focus();
        setTimeout(function(){ map.invalidateSize(); }, 0);
    };
    views.forEach(function(v, i){
        var t = $id("mm-tab-" + v);
        t.onclick = function(){ setView(v, true); };
        t.onkeydown = function(e){
            var n = e.key === "ArrowDown" || e.key === "ArrowRight" ? 1 : e.key === "ArrowUp" || e.key === "ArrowLeft" ? -1 : 0;
            if (!n) return;
            e.preventDefault();
            var next = views[(i + n + views.length) % views.length];
            setView(next);
            $id("mm-tab-" + next).focus();
        };
    });

    // Form
    var droneSel = $id("mp-drone");
    MM.drones.forEach(function(d){
        var o = document.createElement("option");
        o.value = d.code;
        o.textContent = d.name;
        droneSel.appendChild(o);
    });

    var setSeg = function(field, value){
        var g = document.querySelector('.mm-seg[data-field="' + field + '"]');
        Array.prototype.forEach.call(g.querySelectorAll("button"), function(b){
            var on = b.getAttribute("data-value") === value;
            b.setAttribute("aria-pressed", on ? "true" : "false");
            b.classList.toggle("active", on);
            b.classList.toggle("btn-primary", on);
            b.classList.toggle("btn-default", !on);
        });
    };
    var setVal = function(id, v){ var e = $id(id); if (document.activeElement !== e) e.value = v; };
    var setPressed = function(id, on, onText, offText){
        var b = $id(id);
        b.setAttribute("aria-pressed", on ? "true" : "false");
        b.classList.toggle("active", on);
        if (onText) b.querySelector("span").textContent = on ? onText : offText;
    };

    var currentRing = function(){ return S.rings[Math.min(ringIndex, S.rings.length - 1)]; };

    var renderForm = function(){
        var k = S.kind, d = drone(), isFull = full();
        $id("mp-kind").value = k;
        setSeg("capture", S.capture);
        setSeg("path", S.path);
        setSeg("panorama_layout", S.panorama_layout);
        setSeg("oblique_pattern", S.oblique_pattern);
        droneSel.value = S.drone;
        $id("mp-camera").textContent = d.info;
        var alt = k === "orbit" ? currentRing()[1] : S.altitude;
        $id("mp-altitude").min = pointKind() ? 10 : 30;
        setVal("mp-altitude", alt);
        setVal("mp-gsd", round(gsdFor(alt), 2));
        setVal("mp-radius", currentRing()[0]);
        setVal("mp-tilt", currentRing()[2]);
        setVal("mp-side", S.side);
        $id("mp-speed").min = d.min_speed;
        $id("mp-speed").max = d.max_speed;
        setVal("mp-speed", S.speed);
        setVal("mp-direction", S.direction);
        setPressed("mp-auto", S.auto_direction);
        setVal("mp-margin", S.margin);
        setVal("mp-buffer", S.buffer);
        setVal("mp-pitch", S.oblique_pitch);
        $id("mp-nadir").checked = S.oblique_nadir;
        setVal("mp-pano-overlap", S.panorama_overlap);
        setVal("mp-pano-spacing", S.panorama_spacing);
        $id("mp-pano-nadir").checked = S.panorama_nadir;
        $id("mp-spacing-hint").textContent = S.panorama_spacing ? "" : f("Auto (%(m)s m)", {m: Math.round(C.auto_spacing * S.altitude)});
        $id("mp-split").checked = S.split_enabled;
        $id("mp-split-count").disabled = !S.split_enabled;
        setVal("mp-split-count", S.split_count);
        setVal("mp-max-wp", S.max_wp);
        $id("mp-cross").checked = S.cross_hatch;
        $id("mp-reverse").checked = S.reverse;
        $id("mp-reverse").disabled = k === "2d" && S.terrain;
        $id("mp-terrain").checked = S.terrain;
        setVal("mp-terrain-tol", S.terrain_tol);
        $id("mp-finish").value = S.finish;
        $id("mp-rc-lost").value = S.rc_lost;
        setVal("mp-takeoff-tol", S.takeoff_tol);
        setVal("mp-launch-offset", S.launch_offset);
        $id("mp-same-takeoff").checked = S.same_takeoff;
        $id("mp-gsd-var").textContent = f("GSD variance ± %(v)s %", {v: (100 * S.takeoff_tol / S.altitude).toFixed(2)});

        var readout = $id("mp-front-readout"), note = $id("mp-front-note");
        note.textContent = "";
        note.className = "mm-readout";
        if (k === "orbit"){
            readout.textContent = plan && plan.stats && plan.stats.front_overlap_pct != null ? Math.round(plan.stats.front_overlap_pct) + " %" : "—";
        }else if (isFull){
            setVal("mp-front", S.front);
        }else if (!isFull){
            var along = footprintAlong(), pct = (1 - S.speed * d.interval / along) * 100;
            setVal("mp-front", Math.round(pct));
            note.textContent = pct < 60 ? _("Too low") : pct < 70 ? _("Marginal") : "";
            if (pct < 60) note.className = "mm-readout text-danger";
            else if (pct < 70) note.className = "mm-readout text-warning";
        }
        var rings = S.rings.length, suffix = rings > 1 && k === "orbit" ? " " + f("(Ring %(n)s)", {n: ringIndex + 1}) : "";
        $id("mp-altitude-label").textContent = suffix ? f("Altitude, ring %(n)s (m)", {n: ringIndex + 1}) : _("Altitude (m)");
        $id("mp-radius-label").textContent = suffix ? f("Radius, ring %(n)s (m)", {n: ringIndex + 1}) : _("Radius (m)");
        $id("mp-tilt-label").textContent = suffix ? f("Camera tilt, ring %(n)s (°)", {n: ringIndex + 1}) : _("Camera tilt (°)");
        var ring = $id("mp-ring");
        ring.innerHTML = S.rings.map(function(r, i){ return '<option value="' + i + '"' + (i === ringIndex ? " selected" : "") + '>' + esc(f("Ring %(n)s", {n: i + 1})) + '</option>'; }).join("");
        ring.disabled = rings < 2;
        $id("mp-add-ring").disabled = rings >= C.max_rings || !G.centre;
        show("mp-remove-ring", rings > 1);
        show("mp-ring-limit", rings >= C.max_rings);
        layout();
        renderShape();
    };

    var layout = function(){
        var k = S.kind, isFull = full(), area = k === "panorama" && S.panorama_layout === "area";
        show("mp-row-capture", k !== "panorama");
        show("mp-row-path", k !== "orbit");
        show("mp-row-pano-layout", k === "panorama");
        show("mp-area-rings", k === "orbit");
        show("mp-row-gsd", !pointKind());
        show("mp-row-radius", k === "orbit");
        show("mp-row-side", k !== "panorama" && (k !== "orbit" || isFull));
        show("mp-row-front", k !== "panorama" && !(k === "orbit" && isFull));
        $id("mp-front-label").textContent = k === "orbit" ? _("Side overlap (%)") : _("Front overlap (%)");
        show("mp-front", k !== "orbit");
        show("mp-front-readout", k === "orbit");
        show("mp-front-note", !isFull && k !== "orbit" && !!$id("mp-front-note").textContent);
        $id("mp-front-label").setAttribute("for", k !== "orbit" ? "mp-front" : "mp-front-readout");
        $id("mp-front").disabled = !isFull;
        var ftip = $id("mp-front-tip"), ftext = ftip.getAttribute(isFull ? "data-full" : "data-semi");
        ftip.setAttribute("title", ftext);
        ftip.setAttribute("data-original-title", ftext);
        ftip.setAttribute("aria-label", ftext);
        show("mp-row-margin", k === "2d" || k === "oblique");
        show("mp-row-buffer", k === "corridor");
        show("mp-row-direction", k === "2d" || k === "oblique");
        show("mp-rows-oblique", k === "oblique");
        show("mp-rows-pano", k === "panorama");
        show("mp-row-spacing", area);
        show("mp-row-split", k !== "panorama");
        show("mp-row-split-count", k !== "panorama");
        show("mp-row-breaks", k === "corridor");
        show("mp-route-options", k !== "panorama");
        show("mp-row-cross", k === "2d");
        show("mp-row-reverse", k === "2d" || k === "oblique" || k === "orbit");
        show("mp-row-terrain", k !== "orbit" && k !== "panorama");
        show("mp-row-terrain-tol", S.terrain && !isFull && k !== "orbit" && k !== "panorama");
        var tip = k === "orbit" ? _("Fly the ring counterclockwise instead of clockwise. The drone starts at the north point either way.") :
            _("Start from the other end. For 2D without terrain follow, and for Oblique, where each pass starts from its other end.");
        $id("mp-reverse-tip").setAttribute("title", tip);
        $id("mp-reverse-tip").setAttribute("aria-label", tip);
        $id("mp-reverse-tip").setAttribute("data-original-title", tip);
        show("mp-takeoff-na", pointKind());
        show("mp-takeoff-body", !pointKind());
        $id("mp-area-title").textContent = k === "corridor" ? _("Centre line") : k === "orbit" ? _("Orbit") : k === "panorama" ? (area ? _("Survey area") : _("Panorama points")) : _("Survey area");
        var labels = k === "corridor" ? [_("Draw line"), _("Edit")] : k === "orbit" ? [_("Draw circle"), _("Edit")] : (k === "panorama" && !area) ? [_("Place points"), _("Undo")] : [_("Draw area"), _("Edit")];
        var drawing = tool && tool.indexOf("draw") === 0 || tool === "place";
        $id("mp-draw").querySelector("span").textContent = drawing ? _("Finish") : labels[0];
        $id("mp-draw").querySelector(".fa").className = "fa " + (drawing ? "fa-check" : k === "orbit" ? "fa-circle-notch" : k === "panorama" && !area ? "fa-map-marker-alt" : k === "corridor" ? "fa-route" : "fa-draw-polygon");
        var editing = tool && tool.indexOf("edit") === 0;
        $id("mp-edit").querySelector("span").textContent = editing ? _("Finish editing") : labels[1];
        $id("mp-edit").querySelector(".fa").className = "fa " + (editing ? "fa-check" : k === "panorama" && !area ? "fa-undo" : "fa-edit");
        var has = hasArea();
        show("mp-edit", has && !drawing);
        show("mp-remove", has && !drawing && !editing);
        show("mp-import", !drawing && !editing);
        renderExport();
    };

    var renderShape = function(){
        var k = S.kind, txt = "";
        if (k === "orbit") txt = G.centre ? f("Centre %(lat)s, %(lon)s", {lat: G.centre[1].toFixed(6), lon: G.centre[0].toFixed(6)}) : _("No centre point yet.");
        else if (k === "panorama" && S.panorama_layout === "points") txt = G.points.length ? MM.n("%(n)s point", "%(n)s points", G.points.length, {n: G.points.length}) : _("No panorama points yet.");
        else if (k === "corridor") txt = G.line ? (plan && plan.stats && plan.stats.corridor_length_m ? f("Length %(d)s", {d: fmtDistance(plan.stats.corridor_length_m)}) : "") : _("No centre line yet.");
        else txt = G.polygon ? (plan && plan.stats && plan.stats.coverage_ha != null ? f("%(ha)s ha", {ha: plan.stats.coverage_ha.toFixed(1)}) : "") : _("No survey area yet.");
        $id("mp-shape-readout").textContent = txt;
    };

    // Map drawing of the area
    var latlngs = function(coords){ return coords.map(function(p){ return [p[1], p[0]]; }); };

    var drawArea = function(){
        areaLayer.clearLayers();
        var k = S.kind;
        if (polyKind() && G.polygon){
            L.geoJSON(G.polygon, { pane: "mpArea", style: { color: AREA_COLOR, weight: 2, fillOpacity: 0.08, interactive: false } }).addTo(areaLayer);
        }
        if (k === "corridor" && G.line){
            L.geoJSON(G.line, { pane: "mpArea", style: { color: AREA_COLOR, weight: 3, dashArray: "6 4", interactive: false } }).addTo(areaLayer);
        }
        if (k === "orbit" && G.centre){
            var c = [G.centre[1], G.centre[0]];
            S.rings.forEach(function(r, i){
                L.circle(c, { pane: "mpArea", radius: r[0], color: i === ringIndex ? "#FFFFFF" : "#BBBBBB", weight: i === ringIndex ? 2 : 1, dashArray: i === ringIndex ? null : "4 4", fill: false, interactive: false }).addTo(areaLayer);
            });
            var r0 = currentRing()[0];
            var north = L.latLng(c).toBounds(r0 * 2).getNorth();
            L.polyline([c, [north, c[1]]], { pane: "mpArea", color: "#FFFFFF", weight: 1, dashArray: "2 4", interactive: false }).addTo(areaLayer)
                .bindTooltip(Math.round(r0) + " m", { permanent: true, direction: "right", className: "mm-map-label" });
            L.circleMarker(c, { pane: "mpArea", radius: 5, color: "#FFFFFF", weight: 2, fillColor: "#E53935", fillOpacity: 1, interactive: false }).addTo(areaLayer);
        }
        if (k === "panorama" && S.panorama_layout === "points"){
            G.points.forEach(function(p, i){
                if (tool === "place") return;
                L.marker([p[1], p[0]], { pane: "mpEdit", interactive: false, icon: L.divIcon({ className: "mm-num", html: String(i + 1), iconSize: [22, 22] }) }).addTo(areaLayer);
            });
        }
    };

    var drawOverlays = function(){
        overlayLayer.clearLayers();
        if (!plan || !plan.overlays) return;
        if (plan.overlays.band){
            L.geoJSON(plan.overlays.band, { pane: "mpArea", style: { color: "#35c99a", weight: 1, fillColor: "#35c99a", fillOpacity: 0.12, interactive: false } }).addTo(overlayLayer);
        }
        if (plan.overlays.points && S.kind === "panorama" && S.panorama_layout === "area"){
            plan.overlays.points.forEach(function(p, i){
                L.marker([p[1], p[0]], { pane: "mpEdit", interactive: false, icon: L.divIcon({ className: "mm-num", html: String(i + 1), iconSize: [22, 22] }) }).addTo(overlayLayer);
            });
        }
    };

    var flightName = function(i){
        var n = plan.flights.length, lab = plan.flights[i].label;
        return f("Flight %(i)s of %(n)s", {i: i + 1, n: n}) + (lab ? " (" + lab + ")" : "");
    };

    var drawPlan = function(){
        pathLayer.clearLayers();
        dotsLayer.clearLayers();
        if (!plan) return;
        plan.flights.forEach(function(fl, i){
            var pts = latlngs(fl.waypoints), color = FLIGHT_COLORS[i % FLIGHT_COLORS.length];
            if (!pts.length) return;
            L.polyline(pts, { pane: "mpPath", color: "#071018", weight: 5, opacity: 0.85, interactive: false }).addTo(pathLayer);
            L.polyline(pts, { pane: "mpPath", color: color, weight: 2.5, opacity: 1 }).bindTooltip(esc(plan.flights.length > 1 ? flightName(i) : _("Flight path")), { sticky: true }).addTo(pathLayer);
            pts.forEach(function(p, j){
                if (j === 0 || j === pts.length - 1) return;
                L.circleMarker(p, { pane: "mpPath", radius: 2.5, weight: 1, color: "#6FB5FF", fillColor: MID_COLOR, fillOpacity: 1, interactive: false }).addTo(dotsLayer);
            });
            L.circleMarker(pts[pts.length - 1], { pane: "mpPath", radius: 6, weight: 2, color: "#071018", fillColor: END_COLOR, fillOpacity: 1 })
                .bindTooltip(esc(plan.flights.length > 1 ? f("Finish (flight %(i)s)", {i: i + 1}) : _("Finish"))).addTo(pathLayer);
            L.circleMarker(pts[0], { pane: "mpPath", radius: 7, weight: 2, color: "#071018", fillColor: START_COLOR, fillOpacity: 1 })
                .bindTooltip(esc(plan.flights.length > 1 ? f("Start (flight %(i)s)", {i: i + 1}) : _("Start"))).addTo(pathLayer);
        });
    };

    var renderHud = function(){
        var hud = $id("mp-hud");
        if (!plan){ hud.hidden = true; return; }
        var st = plan.stats, k = S.kind, rows = [];
        var ctx = [];
        if (st.coverage_ha != null) ctx.push(st.coverage_ha.toFixed(1) + " ha");
        if (!pointKind()) ctx.push(st.gsd.toFixed(2) + " cm");
        $id("mp-hud-context").textContent = ctx.join(" · ");
        rows.push([_("Flight time"), fmtDuration(st.flight_s), _("Estimated flight time from path length and speed. Takeoff, landing and battery swaps are not included.")]);
        rows.push([_("Batteries"), String(st.batteries), _("Flights needed, one battery each.")]);
        rows.push([_("Distance"), fmtDistance(st.distance_m), _("Total distance along all flight lines.")]);
        if (k === "corridor") rows.push([_("Corridor length"), fmtDistance(st.corridor_length_m), _("Length of the centre line.")]);
        rows.push([_("Waypoints"), fmtInt(st.waypoints), _("Turn points in semi-automatic, one per photo in full-automatic.")]);
        rows.push([_("Photos"), fmtInt(st.photos) + (st.photos_estimated ? " " + _("estimated") : ""), _("Photos the camera will take.")]);
        if (st.front_overlap_pct != null && k !== "orbit") rows.push([_("Front overlap"), Math.round(st.front_overlap_pct) + "%", _("Overlap between consecutive photos.")]);
        rows.push([k === "orbit" ? _("Rings") : k === "panorama" ? (/row/.test(st.lines) ? _("Rows") : _("Points")) : _("Strips"), st.lines, k === "orbit" || k === "panorama" ? "" : _("Parallel flight strips.")]);
        $id("mp-hud-list").innerHTML = rows.map(function(r){
            return '<div class="mm-hud-row"' + (r[2] ? ' title="' + esc(r[2]) + '"' : '') + '><dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd></div>';
        }).join("");
        var info = $id("mp-hud-info");
        info.textContent = plan.info || "";
        info.hidden = !plan.info;
        hud.hidden = false;
    };

    var hudOpen = true;
    try{ hudOpen = localStorage.getItem("mp-hud") !== "closed"; }catch(e){}
    var setHud = function(open){
        hudOpen = open;
        $id("mp-hud").classList.toggle("mm-hud-closed", !open);
        $id("mp-hud-toggle").setAttribute("aria-expanded", open ? "true" : "false");
        try{ localStorage.setItem("mp-hud", open ? "open" : "closed"); }catch(e){}
    };
    $id("mp-hud-toggle").onclick = function(){ setHud(!hudOpen); };
    setHud(hudOpen);

    var exportIssue = function(){
        if (!plan) return _("Plan a mission first.");
        if (S.terrain && plan.terrain_failed) return _("Terrain data could not be loaded. Turn terrain follow off and on to retry; a flat mission is not exported instead.");
        if (plan.errors && plan.errors.length) return plan.errors.join(" ");
        return null;
    };

    var renderExport = function(){
        var n = plan ? plan.flights.length : 0, part = $id("mp-part"), prev = part.value;
        var opts = [];
        if (n > 1) opts.push(['all', f("All %(n)s flights (ZIP)", {n: n})]);
        for (var i = 0; i < n; i++) opts.push([String(i), n > 1 ? flightName(i) : _("Flight")]);
        part.innerHTML = opts.map(function(o){ return '<option value="' + o[0] + '">' + esc(o[1]) + '</option>'; }).join("");
        if (opts.some(function(o){ return o[0] === prev; })) part.value = prev;
        show("mp-row-part", n > 1);
        var issue = exportIssue();
        $id("mp-download").disabled = !!issue;
        $id("mp-rc-download").disabled = !!issue;
        setMsg("mp-export-msg", plan && issue ? issue : "", "warning", plan && S.terrain && plan.terrain_failed ? function(){ replan(); } : null);
        var canSave = !mission ? (MM.projects.length || MM.canAddProject) : mission.can_change;
        show("mp-row-project", !mission || !mission.can_change);
        show("mp-save-new", !!mission && (MM.projects.length || MM.canAddProject));
        show("mp-delete", !!mission && mission.can_change);
        show("mp-readonly", !!mission && !mission.can_change);
        $id("mp-save").disabled = !canSave || !plan;
        $id("mp-save-top").disabled = !canSave || !plan || (!!mission && !dirty);
        show("mp-rc-usb", MM.rc);
        show("mp-rc-manual", !MM.rc);
        $id("mp-rc-send").disabled = !!issue || !rcSelected;
    };

    var renderTitle = function(){
        $id("mp-title").textContent = mission ? mission.name : _("New mission");
        var sub = [];
        if (mission) sub.push(mission.project.name);
        if (dirty) sub.push(_("Unsaved changes"));
        $id("mp-subtitle").textContent = sub.join(" · ");
        document.title = (mission ? mission.name : _("New mission")) + " - " + _("Mission Planner");
    };

    var renderAll = function(){
        renderForm();
        drawArea();
        drawOverlays();
        drawPlan();
        renderHud();
        renderTitle();
    };

    // Planning
    var setBusy = function(on){
        clearTimeout(busyTimer);
        if (on) busyTimer = setTimeout(function(){ $id("mp-busy").hidden = false; }, 300);
        else $id("mp-busy").hidden = true;
    };

    var applyPlan = function(r){
        plan = r;
        if (r.split){
            $id("mp-split-count").max = r.split.max;
            if (!S.split_overridden || S.split_count > r.split.max) S.split_count = r.split.value;
        }
        if (S.auto_direction && r.direction != null) S.direction = r.direction;
        renderAll();
        refreshTakeoff();
        if (S.terrain && r.terrain_failed){
            if (!terrainRetried){
                terrainRetried = true;
                planTimer = setTimeout(replan, 3000);
            }
        }else terrainRetried = false;
    };
    var terrainRetried = false;

    var replan = function(retried){
        clearTimeout(planTimer);
        var seq = ++planSeq;
        if (!hasArea()){
            plan = null;
            setBusy(false);
            setMsg("mp-area-msg", "");
            renderAll();
            clearTakeoff();
            return;
        }
        setBusy(true);
        MM.api("POST", "plan", { settings: S, geometry: geometryFor() }).then(function(r){
            if (seq !== planSeq) return;
            setBusy(false);
            setMsg("mp-area-msg", "");
            applyPlan(r);
        }).catch(function(e){
            if (seq !== planSeq) return;
            if (!e.status && retried !== true){
                planTimer = setTimeout(function(){ replan(true); }, 1500);
                return;
            }
            setBusy(false);
            plan = null;
            var again = e.status ? null : function(){ replan(); };
            setMsg("mp-area-msg", e.message, "warning", again);
            renderAll();
            mapToast(e.message, again);
        });
    };

    var changed = function(now){
        dirty = true;
        renderForm();
        drawArea();
        renderTitle();
        clearTimeout(planTimer);
        planTimer = setTimeout(replan, now ? 0 : 300);
    };

    var toastTimer = null;
    var mapToast = function(text, retry){
        var t = $id("mp-toast");
        if (!t){
            t = document.createElement("div");
            t.id = "mp-toast";
            t.className = "mm-toast";
            t.setAttribute("role", "status");
            map.getContainer().parentNode.appendChild(t);
        }
        t.textContent = text;
        if (retry) t.appendChild(retryLink(function(){ t.hidden = true; retry(); }));
        t.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(function(){ t.hidden = true; }, retry ? 15000 : 6000);
    };

    // Inputs
    var num = function(id, lo, hi, def){
        var v = parseFloat($id(id).value);
        if (isNaN(v)) return def;
        return Math.min(hi, Math.max(lo, v));
    };
    var onInput = function(id, fn){
        var e = $id(id);
        e.addEventListener("change", function(){ fn(true); });
        if (e.tagName === "INPUT" && e.type === "number") e.addEventListener("input", function(){ if ($id(id).value !== "") fn(false); });
    };

    $id("mp-kind").onchange = function(){ setKind(this.value); };
    Array.prototype.forEach.call(document.querySelectorAll(".mm-seg"), function(g){
        var field = g.getAttribute("data-field");
        Array.prototype.forEach.call(g.querySelectorAll("button"), function(b){
            b.onclick = function(){
                var v = b.getAttribute("data-value");
                if (S[field] === v) return;
                if (field === "panorama_layout"){ setPanoramaLayout(v); return; }
                S[field] = v;
                changed(true);
            };
        });
    });
    droneSel.onchange = function(){
        S.drone = this.value;
        var d = drone();
        S.speed = Math.min(d.max_speed, Math.max(d.min_speed, S.speed));
        changed(true);
    };
    onInput("mp-altitude", function(final){
        var v = num("mp-altitude", pointKind() ? 10 : 30, 500, S.altitude);
        if (!final && (v !== parseFloat($id("mp-altitude").value))) return;
        if (S.kind === "orbit"){ currentRing()[1] = v; S.altitude = S.rings[0][1]; if (ringIndex === 0) S.altitude = v; }
        else S.altitude = v;
        if (final) $id("mp-altitude").value = v;
        changed();
    });
    onInput("mp-gsd", function(final){
        var g = parseFloat($id("mp-gsd").value);
        if (!(g > 0)) return;
        var d = drone(), alt = g * d.f * d.iw / (d.sw * 100);
        alt = Math.round(Math.min(500, Math.max(pointKind() ? 10 : 30, alt)) * 10) / 10;
        if (S.kind === "orbit") currentRing()[1] = alt;
        S.altitude = S.kind === "orbit" ? S.rings[0][1] : alt;
        if (final) $id("mp-gsd").value = round(gsdFor(alt), 2);
        $id("mp-altitude").value = alt;
        changed();
    });
    onInput("mp-radius", function(final){ var v = num("mp-radius", 5, 2000, currentRing()[0]); if (!final && v !== parseFloat($id("mp-radius").value)) return; currentRing()[0] = v; changed(); });
    onInput("mp-tilt", function(final){ var v = Math.round(num("mp-tilt", -90, 0, currentRing()[2])); if (!final && v !== parseFloat($id("mp-tilt").value)) return; currentRing()[2] = v; changed(); });
    var simple = function(id, key, lo, hi, intOnly){
        onInput(id, function(final){
            var raw = parseFloat($id(id).value), v = num(id, lo, hi, S[key]);
            if (intOnly) v = Math.round(v);
            if (!final && v !== raw) return;
            if (final) $id(id).value = v;
            S[key] = v;
            if (key === "split_count") S.split_overridden = true;
            changed();
        });
    };
    simple("mp-side", "side", 50, 95, true);
    onInput("mp-front", function(final){
        var raw = parseFloat($id("mp-front").value), v = Math.round(num("mp-front", 50, 95, S.front));
        if (!full() || (!final && v !== raw)) return;
        if (final) $id("mp-front").value = v;
        S.front = v;
        changed();
    });
    onInput("mp-speed", function(final){ var d = drone(), raw = parseFloat($id("mp-speed").value), v = num("mp-speed", d.min_speed, d.max_speed, S.speed); if (!final && v !== raw) return; S.speed = Math.round(v * 10) / 10; if (final) $id("mp-speed").value = S.speed; changed(); });
    onInput("mp-direction", function(final){
        var raw = parseFloat($id("mp-direction").value), v = num("mp-direction", 0, 359.9, S.direction);
        if (!final && v !== raw) return;
        S.direction = v;
        S.auto_direction = false;
        changed();
    });
    $id("mp-auto").onclick = function(){ S.auto_direction = !S.auto_direction; changed(true); };
    simple("mp-margin", "margin", 0, 200);
    simple("mp-buffer", "buffer", 1, 500);
    simple("mp-pitch", "oblique_pitch", -85, -20, true);
    simple("mp-pano-overlap", "panorama_overlap", 20, 70, true);
    simple("mp-pano-spacing", "panorama_spacing", 0, 5000, true);
    simple("mp-split-count", "split_count", 1, 999, true);
    simple("mp-max-wp", "max_wp", 2, 200, true);
    simple("mp-terrain-tol", "terrain_tol", 1, 100, true);
    simple("mp-launch-offset", "launch_offset", -50, 50);
    onInput("mp-takeoff-tol", function(final){
        var raw = parseFloat($id("mp-takeoff-tol").value), v = num("mp-takeoff-tol", 0.1, 50, S.takeoff_tol);
        if (!final && v !== raw) return;
        S.takeoff_tol = Math.round(v * 10) / 10;
        dirty = true;
        renderForm();
        renderTitle();
        if (final) refreshTakeoff(true);
    });
    var check = function(id, key, after){
        $id(id).onchange = function(){
            S[key] = this.checked;
            if (after) after();
            changed(true);
        };
    };
    check("mp-nadir", "oblique_nadir");
    check("mp-pano-nadir", "panorama_nadir");
    check("mp-split", "split_enabled", function(){ S.split_overridden = true; });
    check("mp-cross", "cross_hatch");
    check("mp-reverse", "reverse");
    check("mp-terrain", "terrain", function(){ if (S.terrain && S.kind === "2d") S.reverse = false; });
    check("mp-same-takeoff", "same_takeoff");
    $id("mp-finish").onchange = function(){ S.finish = this.value; changed(true); };
    $id("mp-rc-lost").onchange = function(){ S.rc_lost = this.value; dirty = true; renderTitle(); renderExport(); };

    $id("mp-ring").onchange = function(){ ringIndex = parseInt(this.value, 10) || 0; renderForm(); drawArea(); if (tool === "edit-circle") drawEditHandles(); };
    $id("mp-add-ring").onclick = function(){
        if (S.rings.length >= C.max_rings || !G.centre) return;
        var last = S.rings[S.rings.length - 1];
        S.rings.push([last[0], Math.min(500, last[1] + 15), Math.max(-90, last[2] - 15)]);
        ringIndex = S.rings.length - 1;
        changed(true);
        startTool("edit-circle");
    };
    $id("mp-remove-ring").onclick = function(){
        if (S.rings.length < 2) return;
        S.rings.splice(ringIndex, 1);
        ringIndex = Math.max(0, ringIndex - 1);
        S.altitude = S.rings[0][1];
        changed(true);
    };

    var setKind = function(k){
        var prev = S.kind;
        if (k === prev) return;
        stopTool();
        if (prev === "orbit"){
            if (saved.side != null){ S.side = saved.side; saved.side = null; }
            if (saved.orbitWp != null){ S.max_wp = saved.orbitWp; saved.orbitWp = null; }
            G.centre = null;
            S.rings = [S.rings[0]];
            ringIndex = 0;
        }
        if (prev === "panorama"){
            if (saved.panoWp != null){ S.max_wp = saved.panoWp; saved.panoWp = null; }
            G.points = [];
        }
        if (prev === "orbit" || k === "orbit") S.reverse = false;
        if (k === "orbit"){
            S.path = "curved";
            S.split_enabled = true;
            S.split_overridden = false;
            S.terrain = false;
            saved.side = S.side;
            S.side = 90;
            saved.orbitWp = S.max_wp;
            S.max_wp = 200;
            S.rings[0][1] = S.altitude;
        }
        if (k === "panorama"){
            S.terrain = false;
            S.reverse = false;
            S.split_enabled = false;
            saved.panoWp = S.max_wp;
            S.max_wp = 200;
        }
        if (k !== "orbit" && k !== "panorama" && S.altitude < 30) S.altitude = 30;
        if (k === "orbit" || k === "panorama") clearTakeoff(true);
        S.kind = k;
        changed(true);
    };

    var setPanoramaLayout = function(v){
        stopTool();
        S.panorama_layout = v;
        if (v === "area") G.points = [];
        else G.polygon = G.polygon;
        changed(true);
    };

    // Map tools
    var drawPts = [], selectedVertex = null, circleStart = null, breaksOn = false;
    var help = function(text){ $id("mp-draw-help").textContent = text || ""; show("mp-draw-help", !!text); };

    var stopTool = function(){
        if (tool === "draw-circle") map.dragging.enable();
        tool = null;
        drawPts = [];
        selectedVertex = null;
        circleStart = null;
        editLayer.clearLayers();
        map.getContainer().classList.remove("mm-drawing");
        map.doubleClickZoom.enable();
        help("");
        layout();
        drawArea();
    };

    var startTool = function(t){
        stopTool();
        if (breaksOn) setBreaks(false);
        tool = t;
        if (t.indexOf("draw") === 0 || t === "place") map.getContainer().classList.add("mm-drawing");
        if (t === "draw-poly" || t === "draw-line") map.doubleClickZoom.disable();
        if (t === "draw-circle") map.dragging.disable();
        help({
            "draw-poly": _("Click to add corners. Double-click, Enter or click the first corner to finish. Backspace undoes, Esc cancels."),
            "draw-line": _("Click to add points along the line. Double-click or Enter finishes. Backspace undoes, Esc cancels."),
            "draw-circle": _("Click the object to orbit, or press and drag to set the radius too. Esc cancels."),
            "edit-poly": _("Drag a corner to move it, drag a midpoint to add one, right-click a corner to remove it."),
            "edit-line": _("Drag a point to move it, drag a midpoint to add one, right-click a point to remove it."),
            "edit-circle": _("Drag the centre to move the circle, or a square handle to resize it."),
            "place": _("Click the map for each panorama, in flight order. Drag a point to move it.")
        }[t]);
        layout();
        drawArea();
        if (t.indexOf("edit") === 0) drawEditHandles();
        if (t === "place") drawPlaceMarkers();
        if (t.indexOf("draw") === 0) redrawSketch();
    };

    var redrawSketch = function(){
        editLayer.clearLayers();
        if (drawPts.length > 1) L.polyline(drawPts, { pane: "mpEdit", color: AREA_COLOR, weight: 2, dashArray: "4 4", interactive: false }).addTo(editLayer);
        drawPts.forEach(function(p, i){
            var first = i === 0 && tool === "draw-poly" && drawPts.length > 2;
            var m = L.circleMarker(p, { pane: "mpEdit", radius: i === 0 && tool === "draw-poly" ? 7 : 4, color: AREA_COLOR, weight: 2, fillColor: "#FFFFFF", fillOpacity: 1, interactive: first, bubblingMouseEvents: false }).addTo(editLayer);
            if (first) m.on("click", finishDraw);
        });
    };

    var finishDraw = function(){
        if (tool === "draw-poly"){
            if (drawPts.length < 3){ help(_("Add at least 3 corners.")); return; }
            var ring = drawPts.map(function(p){ return [round(p.lng, 8), round(p.lat, 8)]; });
            ring.push(ring[0]);
            G.polygon = { type: "Polygon", coordinates: [ring] };
        }else if (tool === "draw-line"){
            if (drawPts.length < 2){ help(_("Add at least 2 points.")); return; }
            G.line = { type: "LineString", coordinates: drawPts.map(function(p){ return [round(p.lng, 8), round(p.lat, 8)]; }) };
            G.breaks = [];
        }
        stopTool();
        changed(true);
        fitArea();
    };

    map.on("click", function(e){
        if (tool === "draw-poly" || tool === "draw-line"){
            drawPts.push(e.latlng);
            redrawSketch();
        }else if (tool === "place"){
            addPoint([round(e.latlng.lng, 8), round(e.latlng.lat, 8)]);
        }
    });
    map.on("dblclick", function(e){
        if (tool === "draw-poly" || tool === "draw-line"){
            var last = drawPts[drawPts.length - 1];
            if (last && map.latLngToContainerPoint(last).distanceTo(e.containerPoint) < 6) drawPts.pop();
            drawPts.pop();
            drawPts.push(e.latlng);
            finishDraw();
        }
    });
    map.on("mousedown", function(e){
        if (tool !== "draw-circle") return;
        circleStart = { latlng: e.latlng, point: e.containerPoint, moved: false };
    });
    map.on("mousemove", function(e){
        if (tool !== "draw-circle" || !circleStart) return;
        if (e.containerPoint.distanceTo(circleStart.point) > 4) circleStart.moved = true;
        if (!circleStart.moved) return;
        editLayer.clearLayers();
        var r = Math.min(2000, Math.max(5, circleStart.latlng.distanceTo(e.latlng)));
        L.circle(circleStart.latlng, { pane: "mpEdit", radius: r, color: "#FFFFFF", weight: 2, fill: false, interactive: false }).addTo(editLayer);
        L.marker(e.latlng, { pane: "mpEdit", interactive: false, icon: L.divIcon({ className: "mm-map-label mm-map-label-static", html: Math.round(r) + " m", iconSize: null }) }).addTo(editLayer);
    });
    map.on("mouseup", function(e){
        if (tool !== "draw-circle" || !circleStart) return;
        var c = circleStart;
        G.centre = [round(c.latlng.lng, 8), round(c.latlng.lat, 8)];
        if (c.moved) currentRing()[0] = Math.round(Math.min(2000, Math.max(5, c.latlng.distanceTo(e.latlng))) * 10) / 10;
        stopTool();
        changed(true);
        if (!map.getBounds().contains(c.latlng)) map.panTo(c.latlng);
    });

    document.addEventListener("keydown", function(e){
        var t = e.target;
        if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) return;
        if (!tool) return;
        if (e.key === "Escape"){ stopTool(); return; }
        if ((tool === "draw-poly" || tool === "draw-line") && e.key === "Enter"){ e.preventDefault(); finishDraw(); }
        if ((tool === "draw-poly" || tool === "draw-line") && e.key === "Backspace"){ e.preventDefault(); drawPts.pop(); redrawSketch(); }
        if ((tool === "edit-poly" || tool === "edit-line") && (e.key === "Delete" || e.key === "Backspace") && selectedVertex){ e.preventDefault(); removeVertex(selectedVertex); }
    });

    var editRings = function(){
        if (tool === "edit-poly"){
            if (G.polygon.type === "Polygon") return G.polygon.coordinates.map(function(r){ return { ring: r, closed: true }; });
            var out = [];
            G.polygon.coordinates.forEach(function(p){ p.forEach(function(r){ out.push({ ring: r, closed: true }); }); });
            return out;
        }
        if (G.line.type === "LineString") return [{ ring: G.line.coordinates, closed: false }];
        return G.line.coordinates.map(function(r){ return { ring: r, closed: false }; });
    };

    var handleIcon = function(cls){ return L.divIcon({ className: "mm-handle " + (cls || ""), iconSize: [14, 14] }); };

    var drawEditHandles = function(){
        editLayer.clearLayers();
        if (tool === "edit-circle"){
            if (!G.centre) return;
            var c = L.latLng(G.centre[1], G.centre[0]), r = currentRing()[0];
            var circle = L.circle(c, { pane: "mpEdit", radius: r, color: "#FFFFFF", weight: 2, fill: false, interactive: false }).addTo(editLayer);
            var centre = L.marker(c, { pane: "mpEdit", draggable: true, icon: handleIcon("mm-handle-centre"), title: _("Move") }).addTo(editLayer);
            var handles = [0, 90, 180, 270].map(function(b){
                var m = L.marker(destination(c, b, r), { pane: "mpEdit", draggable: true, icon: handleIcon(), title: _("Resize") }).addTo(editLayer);
                m.on("drag", function(e){
                    var nr = Math.min(2000, Math.max(5, c.distanceTo(e.target.getLatLng())));
                    circle.setRadius(nr);
                });
                m.on("dragend", function(e){
                    currentRing()[0] = Math.round(Math.min(2000, Math.max(5, c.distanceTo(e.target.getLatLng()))) * 10) / 10;
                    drawEditHandles();
                    changed(true);
                });
                return m;
            });
            centre.on("drag", function(e){
                c = e.target.getLatLng();
                circle.setLatLng(c);
                handles.forEach(function(h, i){ h.setLatLng(destination(c, i * 90, circle.getRadius())); });
            });
            centre.on("dragend", function(e){
                var p = e.target.getLatLng();
                G.centre = [round(p.lng, 8), round(p.lat, 8)];
                drawEditHandles();
                changed(true);
            });
            return;
        }
        var shape = null;
        var redrawShape = function(){
            if (shape) editLayer.removeLayer(shape);
            if (tool === "edit-poly") shape = L.geoJSON(G.polygon, { pane: "mpEdit", style: { color: "#FFFFFF", weight: 2, fillOpacity: 0.05, interactive: false } }).addTo(editLayer);
            else shape = L.geoJSON(G.line, { pane: "mpEdit", style: { color: "#FFFFFF", weight: 2, interactive: false } }).addTo(editLayer);
        };
        redrawShape();
        editRings().forEach(function(er){
            var ring = er.ring, n = er.closed ? ring.length - 1 : ring.length;
            for (var i = 0; i < n; i++){
                (function(i){
                    var m = L.marker([ring[i][1], ring[i][0]], { pane: "mpEdit", draggable: true, icon: handleIcon(selectedVertex && selectedVertex.ring === ring && selectedVertex.i === i ? "mm-handle-on" : ""), keyboard: false }).addTo(editLayer);
                    m.on("drag", function(e){
                        var p = e.target.getLatLng();
                        ring[i] = [round(p.lng, 8), round(p.lat, 8)];
                        if (er.closed && i === 0) ring[ring.length - 1] = ring[0];
                        redrawShape();
                    });
                    m.on("dragend", function(){ drawEditHandles(); changed(true); });
                    m.on("click", function(){ selectedVertex = { ring: ring, i: i, closed: er.closed }; drawEditHandles(); });
                    m.on("contextmenu", function(e){ L.DomEvent.preventDefault(e.originalEvent); removeVertex({ ring: ring, i: i, closed: er.closed }); });
                })(i);
            }
            var segs = er.closed ? ring.length - 1 : ring.length - 1;
            for (var j = 0; j < segs; j++){
                (function(j){
                    var a = ring[j], b = ring[j + 1];
                    var mid = L.marker([(a[1] + b[1]) / 2, (a[0] + b[0]) / 2], { pane: "mpEdit", draggable: true, icon: handleIcon("mm-handle-mid"), keyboard: false }).addTo(editLayer);
                    var inserted = false;
                    mid.on("dragstart", function(){
                        var p = mid.getLatLng();
                        ring.splice(j + 1, 0, [round(p.lng, 8), round(p.lat, 8)]);
                        inserted = true;
                    });
                    mid.on("drag", function(e){
                        var p = e.target.getLatLng();
                        ring[j + 1] = [round(p.lng, 8), round(p.lat, 8)];
                        redrawShape();
                    });
                    mid.on("dragend", function(){ if (inserted){ shiftBreaks(j + 1); drawEditHandles(); changed(true); } });
                    mid.on("click", function(){
                        var p = mid.getLatLng();
                        ring.splice(j + 1, 0, [round(p.lng, 8), round(p.lat, 8)]);
                        shiftBreaks(j + 1);
                        drawEditHandles();
                        changed(true);
                    });
                })(j);
            }
        });
    };

    var shiftBreaks = function(at){
        if (tool !== "edit-line") return;
        G.breaks = G.breaks.map(function(b){ return b[1] >= at ? [b[0], b[1] + 1] : b; });
    };

    var removeVertex = function(v){
        var min = v.closed ? 4 : 2;
        if (v.ring.length - 1 < min && v.closed){ mapToast(_("An area needs at least 3 corners.")); return; }
        if (!v.closed && v.ring.length <= min){ mapToast(_("A line needs at least 2 points.")); return; }
        v.ring.splice(v.i, 1);
        if (v.closed){
            if (v.i === 0) v.ring[v.ring.length - 1] = v.ring[0];
        }else{
            G.breaks = G.breaks.filter(function(b){ return b[1] !== v.i; }).map(function(b){ return b[1] > v.i ? [b[0], b[1] - 1] : b; });
        }
        selectedVertex = null;
        drawEditHandles();
        changed(true);
    };

    var destination = function(c, bearing, dist){
        var R = 6378137, d = dist / R, b = bearing * Math.PI / 180;
        var lat1 = c.lat * Math.PI / 180, lon1 = c.lng * Math.PI / 180;
        var lat2 = Math.asin(Math.sin(lat1) * Math.cos(d) + Math.cos(lat1) * Math.sin(d) * Math.cos(b));
        var lon2 = lon1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(lat1), Math.cos(d) - Math.sin(lat1) * Math.sin(lat2));
        return L.latLng(lat2 * 180 / Math.PI, lon2 * 180 / Math.PI);
    };

    var checkPoints = function(points){
        return MM.api("POST", "panorama-check", { settings: S, geometry: {}, points: points }).then(function(r){ return r.problem; }, function(){ return null; });
    };

    var addPoint = function(p){
        var next = G.points.concat([p]);
        checkPoints(next).then(function(problem){
            if (problem){ mapToast(problem); return; }
            G.points = next;
            drawPlaceMarkers();
            changed(true);
        });
    };

    var drawPlaceMarkers = function(){
        editLayer.clearLayers();
        G.points.forEach(function(p, i){
            var m = L.marker([p[1], p[0]], { pane: "mpEdit", draggable: true, icon: L.divIcon({ className: "mm-num mm-num-edit", html: String(i + 1), iconSize: [24, 24] }), title: f("Point %(n)s", {n: i + 1}) }).addTo(editLayer);
            m.on("dragend", function(e){
                var q = e.target.getLatLng(), next = G.points.slice();
                next[i] = [round(q.lng, 8), round(q.lat, 8)];
                checkPoints(next).then(function(problem){
                    if (problem){ mapToast(problem); drawPlaceMarkers(); return; }
                    G.points = next;
                    drawPlaceMarkers();
                    changed(true);
                });
            });
        });
    };

    var setBreaks = function(on){
        breaksOn = on;
        setPressed("mp-breaks", on, _("Finish breaks"), _("Set mission breaks"));
        show("mp-breaks-help", on);
        drawBreaks();
    };

    var breaksLayer = L.layerGroup().addTo(map);
    var drawBreaks = function(){
        breaksLayer.clearLayers();
        if (S.kind !== "corridor" || !G.line) return;
        var parts = G.line.type === "LineString" ? [G.line.coordinates] : G.line.coordinates;
        parts.forEach(function(part, pi){
            for (var vi = 1; vi < part.length - 1; vi++){
                (function(vi){
                    var on = G.breaks.some(function(b){ return b[0] === pi && b[1] === vi; });
                    if (!on && !breaksOn) return;
                    var m = L.circleMarker([part[vi][1], part[vi][0]], { pane: "mpEdit", radius: on ? 8 : 6, color: "#FFFFFF", weight: 2, fillColor: on ? "#E53935" : "#607D8B", fillOpacity: 1, interactive: breaksOn, bubblingMouseEvents: false }).addTo(breaksLayer);
                    m.bindTooltip(esc(on ? _("Mission break") : _("Click to break here")));
                    if (breaksOn) m.on("click", function(){
                        if (on) G.breaks = G.breaks.filter(function(b){ return !(b[0] === pi && b[1] === vi); });
                        else G.breaks.push([pi, vi]);
                        drawBreaks();
                        changed(true);
                    });
                })(vi);
            }
        });
    };
    $id("mp-breaks").onclick = function(){
        if (!G.line){ mapToast(_("Draw the centre line first.")); return; }
        stopTool();
        setBreaks(!breaksOn);
    };

    $id("mp-draw").onclick = function(){
        var k = S.kind, area = k === "panorama" && S.panorama_layout === "area";
        if (tool === "draw-poly" || tool === "draw-line"){ finishDraw(); return; }
        if (tool === "place" || tool === "draw-circle"){ stopTool(); return; }
        if (k === "corridor") startTool("draw-line");
        else if (k === "orbit") startTool("draw-circle");
        else if (k === "panorama" && !area) startTool("place");
        else startTool("draw-poly");
    };
    $id("mp-edit").onclick = function(){
        var k = S.kind;
        if (k === "panorama" && S.panorama_layout === "points"){
            if (!G.points.length) return;
            G.points = G.points.slice(0, -1);
            if (tool === "place") drawPlaceMarkers();
            changed(true);
            return;
        }
        if (tool && tool.indexOf("edit") === 0){ stopTool(); return; }
        startTool(k === "corridor" ? "edit-line" : k === "orbit" ? "edit-circle" : "edit-poly");
    };
    $id("mp-remove").onclick = function(){
        var k = S.kind;
        stopTool();
        if (k === "corridor"){ G.line = null; G.breaks = []; setBreaks(false); }
        else if (k === "orbit"){ G.centre = null; S.rings = [S.rings[0]]; ringIndex = 0; }
        else if (k === "panorama" && S.panorama_layout === "points") G.points = [];
        else G.polygon = null;
        clearTakeoff(true);
        changed(true);
    };

    $id("mp-file").onchange = function(e){
        var file = e.target.files[0];
        e.target.value = "";
        if (!file) return;
        stopTool();
        MM.upload("import", file).then(function(r){
            var k = S.kind, ok = false;
            if (k === "corridor" && r.lines.length){
                G.line = r.lines.length === 1 ? { type: "LineString", coordinates: r.lines[0] } : { type: "MultiLineString", coordinates: r.lines };
                G.breaks = [];
                ok = true;
            }else if (k === "orbit" && (r.points.length || r.polygons.length)){
                var p = r.points[0];
                if (!p){
                    var ring = r.polygons[0][0], sx = 0, sy = 0;
                    ring.slice(0, -1).forEach(function(q){ sx += q[0]; sy += q[1]; });
                    p = [sx / (ring.length - 1), sy / (ring.length - 1)];
                }
                G.centre = [round(p[0], 8), round(p[1], 8)];
                ok = true;
            }else if (k === "panorama" && S.panorama_layout === "points" && r.points.length){
                G.points = r.points.slice(0, 50).map(function(q){ return [round(q[0], 8), round(q[1], 8)]; });
                ok = true;
            }else if (polyKind() && r.polygons.length){
                G.polygon = r.polygons.length === 1 ? { type: "Polygon", coordinates: r.polygons[0] } : { type: "MultiPolygon", coordinates: r.polygons };
                ok = true;
            }
            if (!ok){
                setMsg("mp-area-msg", f("No usable %(what)s found in %(file)s.", {what: k === "corridor" ? _("line") : k === "orbit" ? _("point") : (k === "panorama" && S.panorama_layout === "points") ? _("points") : _("area"), file: file.name}), "warning");
                return;
            }
            setMsg("mp-area-msg", "");
            clearTakeoff(true);
            changed(true);
            fitArea();
        }).catch(function(err){ setMsg("mp-area-msg", err.message, "warning"); });
    };

    var fitArea = function(){
        var b = null;
        var add = function(lat, lng){ var ll = L.latLng(lat, lng); b = b ? b.extend(ll) : L.latLngBounds(ll, ll); };
        var walk = function(c){ if (typeof c[0] === "number") add(c[1], c[0]); else c.forEach(walk); };
        var k = S.kind;
        if (k === "corridor" && G.line) walk(G.line.coordinates);
        else if (k === "orbit" && G.centre){
            var c = L.latLng(G.centre[1], G.centre[0]), r = Math.max.apply(null, S.rings.map(function(x){ return x[0]; }));
            b = c.toBounds(r * 2.4);
        }else if (k === "panorama" && S.panorama_layout === "points" && G.points.length) walk(G.points);
        else if (G.polygon) walk(G.polygon.coordinates);
        if (b) map.fitBounds(b, { padding: [40, 40], maxZoom: 18 });
    };

    var takeoffOn = false, takeoffSeq = 0, takeoffTimer = null;
    var clearTakeoff = function(reset){
        if (takeoffLayer){ map.removeLayer(takeoffLayer); takeoffLayer = null; }
        if (reset){
            takeoffOn = false;
            setPressed("mp-show-takeoff", false, _("Hide takeoff zone"), _("Show takeoff zone"));
            setMsg("mp-takeoff-msg", "");
        }
    };
    var refreshTakeoff = function(now){
        clearTimeout(takeoffTimer);
        if (!takeoffOn) return;
        takeoffTimer = setTimeout(loadTakeoff, now ? 0 : 400);
    };
    var loadTakeoff = function(){
        var seq = ++takeoffSeq;
        if (!plan){ clearTakeoff(); return; }
        var body = { settings: S, geometry: geometryFor(), flights: plan.flights.map(function(fl){ return { waypoints: fl.waypoints }; }) };
        var msgs = [];
        var jobs = [];
        if (takeoffOn) jobs.push(MM.api("POST", "takeoff", body).then(function(r){
            if (seq !== takeoffSeq) return;
            if (takeoffLayer) map.removeLayer(takeoffLayer);
            takeoffLayer = L.layerGroup().addTo(map);
            r.zones.forEach(function(z){
                var col = TAKEOFF_COLORS[z.index % TAKEOFF_COLORS.length];
                L.geoJSON(z.geometry, { pane: "mpArea", style: { color: col, weight: 1.5, fillColor: col, fillOpacity: 0.47 } })
                    .bindTooltip(esc((r.zones.length > 1 ? f("Takeoff %(n)s", {n: z.index + 1}) : _("Takeoff zone")) + " · " + f("%(e)s m", {e: z.ref_elev})), { sticky: true }).addTo(takeoffLayer);
            });
            msgs.push(MM.n("Takeoff zone: %(n)s area within %(r)s m of the first waypoint, ± %(v)s% GSD variance.", "Takeoff zone: %(n)s areas within %(r)s m of the first waypoint, ± %(v)s% GSD variance.", r.zones.length, {n: r.zones.length, r: Math.round(r.radius_m), v: r.gsd_var.toFixed(2)}));
        }, function(e){
            if (seq !== takeoffSeq) return;
            msgs.push(e.message);
            takeoffOn = false;
            setPressed("mp-show-takeoff", false, _("Hide takeoff zone"), _("Show takeoff zone"));
            if (takeoffLayer){ map.removeLayer(takeoffLayer); takeoffLayer = null; }
        }));
        setMsg("mp-takeoff-msg", _("Reading ground elevations..."), "info");
        Promise.all(jobs).then(function(){
            if (seq !== takeoffSeq) return;
            setMsg("mp-takeoff-msg", msgs.join(" "), "info");
        });
    };
    $id("mp-show-takeoff").onclick = function(){
        if (!plan){ setMsg("mp-takeoff-msg", _("Plan a mission first, then show its takeoff zone."), "info"); return; }
        takeoffOn = !takeoffOn;
        setPressed("mp-show-takeoff", takeoffOn, _("Hide takeoff zone"), _("Show takeoff zone"));
        if (!takeoffOn && takeoffLayer){ map.removeLayer(takeoffLayer); takeoffLayer = null; }
        if (takeoffOn) refreshTakeoff(true);
        else setMsg("mp-takeoff-msg", "");
    };

    // Export
    var missionName = function(){ return $id("mp-name").value.trim() || (mission ? mission.name : "") || _("Mission"); };
    var partIndex = function(){ var v = $id("mp-part").value; return v === "all" || v === "" ? null : parseInt(v, 10); };

    $id("mp-download").onclick = function(){
        var btn = this;
        btn.disabled = true;
        MM.download("export", { settings: S, flights: plan.flights, name: missionName(), part: plan.flights.length > 1 ? partIndex() : null }).then(function(){
            btn.disabled = false;
            setMsg("mp-export-msg", "");
        }).catch(function(e){
            btn.disabled = false;
            setMsg("mp-export-msg", e.message, "warning");
        });
    };

    var rcPart = function(){
        var n = plan.flights.length;
        if (n === 1) return 0;
        var p = partIndex();
        if (p === null){ setMsg("mp-rc-msg", _("Choose one flight under Download: the controller replaces one mission at a time."), "warning"); return null; }
        return p;
    };
    var rcName = function(p){
        var n = plan.flights.length;
        return n > 1 ? f("%(name)s %(i)s of %(n)s", {name: missionName(), i: p + 1, n: n}) : missionName();
    };

    $id("mp-rc-download").onclick = function(){
        var p = rcPart();
        if (p === null) return;
        var uuid = $id("mp-uuid").value.trim();
        if (uuid && !UUID_RE.test(uuid)){ setMsg("mp-rc-msg", _("The folder name looks like xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx. Check it and try again."), "warning"); return; }
        var btn = this;
        btn.disabled = true;
        MM.download("export", { settings: S, flights: plan.flights, name: missionName(), part: p, uuid: uuid }).then(function(name){
            btn.disabled = false;
            setMsg("mp-rc-msg", f("Downloaded %(file)s. Follow the steps below to put it on the controller.", {file: name}), "success");
        }).catch(function(e){
            btn.disabled = false;
            setMsg("mp-rc-msg", e.message, "warning");
        });
    };

    var rcPath = null, rcMissions = [], rcSelected = null, rcCrumbs = [];
    var thumb = function(wps){
        if (!wps || wps.length < 2) return '<span class="mm-rc-thumb mm-rc-thumb-empty" aria-hidden="true"><i class="fa fa-route"></i></span>';
        var xs = wps.map(function(p){ return p[0]; }), ys = wps.map(function(p){ return p[1]; });
        var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs), y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
        var k = Math.cos((y0 + y1) / 2 * Math.PI / 180), w = Math.max(1e-9, (x1 - x0) * k), h = Math.max(1e-9, y1 - y0), s = 52 / Math.max(w, h);
        var pts = wps.map(function(p){ return (6 + ((p[0] - x0) * k) * s + (52 - w * s) / 2).toFixed(1) + "," + (6 + (y1 - p[1]) * s + (52 - h * s) / 2).toFixed(1); }).join(" ");
        return '<svg class="mm-rc-thumb" viewBox="0 0 64 64" aria-hidden="true"><polyline points="' + pts + '" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>';
    };
    var renderRc = function(){
        var list = $id("mp-rc-list");
        list.innerHTML = rcMissions.map(function(m, i){
            var sel = rcSelected && rcSelected.uuid === m.uuid;
            return '<li><label class="mm-rc-item' + (sel ? " theme-border-highlight mm-rc-on" : "") + '"><input type="radio" name="mp-rc-pick" value="' + i + '"' + (sel ? " checked" : "") + '>' + thumb(m.waypoints) +
                '<span class="mm-rc-text"><span class="mm-rc-date">' + esc(m.date_str) + '</span><span class="text-muted">' + esc(MM.n("%(n)s waypoint", "%(n)s waypoints", m.n_wp, {n: m.n_wp})) + '</span></span></label></li>';
        }).join("");
        Array.prototype.forEach.call(list.querySelectorAll("input"), function(r){
            r.onchange = function(){ rcSelected = rcMissions[parseInt(r.value, 10)]; renderRc(); renderExport(); };
        });
        renderExport();
    };
    var rcResult = function(r){
        rcPath = r.path;
        rcMissions = r.missions || [];
        rcSelected = null;
        renderRc();
        if (r.status === "ok") setMsg("mp-rc-msg", MM.n("Found %(n)s mission. Choose the one to replace.", "Found %(n)s missions. Choose the one to replace.", rcMissions.length, {n: rcMissions.length}), "info");
        else if (r.status === "no_mission") setMsg("mp-rc-msg", _("The controller has no waypoint missions. Create one in DJI Fly, then detect again."), "warning");
        else if (r.status === "not_connected") setMsg("mp-rc-msg", _("No controller found. Connect it with USB, unlock it, and allow file access."), "warning");
        else setMsg("mp-rc-msg", r.detail || _("Could not read the controller."), "warning");
    };
    $id("mp-rc-detect").onclick = function(){
        var btn = this;
        btn.disabled = true;
        $id("mp-rc-browser").hidden = true;
        setMsg("mp-rc-msg", _("Looking for the controller. This can take a minute..."), "info");
        MM.api("POST", "rc/detect", {}).then(function(r){ btn.disabled = false; rcResult(r); }).catch(function(e){ btn.disabled = false; setMsg("mp-rc-msg", e.message, "warning"); });
    };
    var browse = function(parts){
        var el = $id("mp-rc-browser");
        el.hidden = false;
        el.innerHTML = '<p class="text-muted"><i class="fa fa-circle-notch fa-spin" aria-hidden="true"></i> ' + esc(_("Loading...")) + '</p>';
        MM.api("POST", "rc/browse", { parts: parts }).then(function(r){
            rcCrumbs = parts;
            var h = '<p class="mm-crumbs">' + esc(_("This PC")) + (parts.length ? " › " + parts.map(esc).join(" › ") : "") + '</p><ul class="mm-folders">';
            if (parts.length) h += '<li><button type="button" class="btn btn-default" data-up="1"><i class="fa fa-level-up-alt" aria-hidden="true"></i> ' + esc(_("Up")) + '</button></li>';
            r.children.forEach(function(c, i){
                var label = c.split("\u0000").pop();
                h += '<li><button type="button" class="btn btn-default" data-child="' + i + '"><i class="fa fa-folder" aria-hidden="true"></i> ' + esc(label) + '</button></li>';
            });
            h += '</ul><div class="mm-buttons"><button type="button" class="btn btn-primary" id="mp-rc-use"' + (parts.length ? "" : " disabled") + '><i class="fa fa-check" aria-hidden="true"></i> ' + esc(_("Use this folder")) + '</button></div>';
            el.innerHTML = h;
            Array.prototype.forEach.call(el.querySelectorAll("[data-child]"), function(b){
                b.onclick = function(){ browse(parts.concat([r.children[parseInt(b.getAttribute("data-child"), 10)].split("\u0000")[0]])); };
            });
            var up = el.querySelector("[data-up]");
            if (up) up.onclick = function(){ browse(parts.slice(0, -1)); };
            $id("mp-rc-use").onclick = function(){
                setMsg("mp-rc-msg", _("Reading missions..."), "info");
                MM.api("POST", "rc/browse", { parts: parts, list: true }).then(function(m){ el.hidden = true; rcResult(m); }).catch(function(e){ setMsg("mp-rc-msg", e.message, "warning"); });
            };
        }).catch(function(e){ el.hidden = true; setMsg("mp-rc-msg", e.message, "warning"); });
    };
    $id("mp-rc-browse").onclick = function(){ browse([]); };
    $id("mp-rc-send").onclick = function(){
        if (!rcSelected || !rcPath) return;
        var p = rcPart();
        if (p === null) return;
        var btn = this, target = rcSelected;
        btn.disabled = true;
        setMsg("mp-rc-msg", _("Sending the mission to the controller, please wait..."), "info");
        MM.api("POST", "rc/send", { path: rcPath, uuid: target.uuid, create_ms: target.create_ms, settings: S, flight: plan.flights[p], name: rcName(p) }).then(function(r){
            btn.disabled = false;
            target.waypoints = plan.flights[p].waypoints;
            target.n_wp = r.waypoints;
            renderRc();
            setMsg("mp-rc-msg", f("Replaced the mission from %(date)s with %(n)s waypoints. Reopen DJI Fly on the controller to fly it.", {date: target.date_str, n: fmtInt(r.waypoints)}), "success");
        }).catch(function(e){
            btn.disabled = false;
            setMsg("mp-rc-msg", e.message, "danger");
        });
    };

    // Save
    var projectSel = $id("mp-project");
    var fillProjects = function(){
        var h = "";
        if (MM.canAddProject) h += '<option value="new">' + esc(_("New project...")) + '</option>';
        MM.projects.forEach(function(p){ h += '<option value="' + p.id + '">' + esc(p.name) + '</option>'; });
        projectSel.innerHTML = h;
        if (MM.projects.length) projectSel.value = String(MM.projects[0].id);
    };
    fillProjects();

    var snapshot = function(){ return { settings: S, geometry: G, plan: plan }; };

    var save = function(asNew){
        var name = $id("mp-name").value.trim();
        if (asNew && mission && name === mission.name) name = f("%(name)s copy", {name: name});
        if (!name){ setMsg("mp-save-msg", _("Type a name for the mission."), "warning"); setView("export"); $id("mp-name").focus(); return; }
        if (!plan){ setMsg("mp-save-msg", _("Plan the mission before saving it."), "warning"); return; }
        var body = snapshot();
        body.name = name;
        var req;
        if (!mission || asNew){
            body.project = mission && mission.can_change ? mission.project.id : projectSel.value;
            req = MM.api("POST", "missions", body);
        }else{
            body.revision = mission.revision;
            req = MM.api("PUT", "missions/" + mission.id, body);
        }
        $id("mp-save").disabled = $id("mp-save-top").disabled = true;
        req.then(function(r){
            var created = !mission || asNew;
            setMission(r, true);
            setMsg("mp-save-msg", created ? f("Saved \"%(name)s\" in %(project)s.", {name: r.name, project: r.project.name}) : _("Changes saved."), "success");
            if (created) history.replaceState(null, "", MM.pluginUrl + r.id + "/");
        }).catch(function(e){
            renderExport();
            if (e.status === 409) return conflict();
            setMsg("mp-save-msg", e.message, "warning");
            setView("export");
        });
    };

    var conflict = function(){
        MM.choose(_("This mission changed since you opened it."), _("Your edits are still here."), [
            { key: "reload", label: _("Load latest"), icon: "fa-sync-alt" },
            { key: "copy", label: _("Save as new"), icon: "fa-copy", primary: true },
            { key: null, label: _("Cancel"), icon: "fa-times", cancel: true }
        ]).then(function(choice){
            if (choice === "copy") save(true);
            else if (choice === "reload") MM.confirm(_("Load the latest mission?"), _("Your edits will be discarded."), _("Load latest"), _("Cancel")).then(function(yes){ if (yes) loadMission(mission.id); });
        });
    };

    $id("mp-save").onclick = function(){ save(false); };
    $id("mp-save-top").onclick = function(){ save(false); };
    $id("mp-save-new").onclick = function(){ save(true); };
    $id("mp-delete").onclick = function(){
        MM.choose(f("Delete \"%(name)s\"?", {name: mission.name}), _("The mission is removed for everyone. This cannot be undone."), [
            { key: true, label: _("Delete"), icon: "fa-trash", danger: true },
            { key: false, label: _("Cancel"), icon: "fa-times", cancel: true }
        ]).then(function(yes){
            if (!yes) return;
            MM.api("DELETE", "missions/" + mission.id).then(function(){
                dirty = false;
                window.location.href = MM.pluginUrl;
            }).catch(function(e){ setMsg("mp-save-msg", e.message, "warning"); });
        });
    };
    $id("mp-name").addEventListener("input", function(){ if (mission) { dirty = true; renderTitle(); renderExport(); } });

    var setMission = function(m, keepPlan){
        mission = m;
        dirty = false;
        $id("mp-name").value = m.name;
        if (!keepPlan){
            S = Object.assign(clone(C.defaults), m.settings);
            G = Object.assign({ polygon: null, line: null, centre: null, points: [], breaks: [] }, m.geometry);
            plan = m.plan;
            ringIndex = 0;
        }
        renderAll();
    };

    var loadMission = function(id){
        stopTool();
        clearTakeoff(true);
        return MM.api("GET", "missions/" + id).then(function(m){
            setMission(m);
            fitArea();
        }).catch(function(e){
            $id("mp-title").textContent = _("Mission not found");
            setMsg("mp-save-msg", e.message, "warning");
        });
    };

    document.addEventListener("click", function(e){
        var a = e.target.closest && e.target.closest("a[href]");
        if (!dirty || !a || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || a.target === "_blank") return;
        var href = a.getAttribute("href");
        if (!href || href.charAt(0) === "#" || /^javascript:/i.test(href) || a.closest(".leaflet-container")) return;
        e.preventDefault();
        MM.choose(_("Leave without saving?"), _("Your changes to this mission will be lost."), [
            { key: true, label: _("Leave"), icon: "fa-sign-out-alt", danger: true },
            { key: false, label: _("Stay"), icon: "fa-times", cancel: true }
        ]).then(function(leave){
            if (!leave) return;
            dirty = false;
            window.location.href = a.href;
        });
    }, true);

    var initial = "mission";
    try{ initial = localStorage.getItem("mp-view") || "mission"; }catch(e){}
    if (views.indexOf(initial) < 0) initial = "mission";
    setView(initial);
    if (!MM.projects.length && !MM.canAddProject) setMsg("mp-save-msg", _("You need a project you can edit to save missions."), "info");
    if (MM.missionId) loadMission(MM.missionId);
    else renderAll();
    MM.tips(document);
})();
