(function(){
    var gt = window.gettext || function(s){ return s; };
    var fmt = window.interpolate || function(s, o){ return s.replace(/%\((\w+)\)s/g, function(_, k){ return o[k]; }); };

    MM._ = gt;
    MM.f = function(s, o){ return fmt(gt(s), o, true); };
    var ngt = window.ngettext || function(a, b, n){ return n === 1 ? a : b; };
    MM.n = function(one, many, n, o){ return fmt(ngt(one, many, n), o, true); };

    MM.csrf = function(){
        var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : "";
    };

    MM.errorText = function(j){
        if (j == null) return null;
        if (typeof j === "string") return j;
        if (Array.isArray(j)) return MM.errorText(j[0]);
        if (typeof j === "object"){
            if (j.detail) return MM.errorText(j.detail);
            if (j.error) return MM.errorText(j.error);
            var k = Object.keys(j)[0];
            return k ? MM.errorText(j[k]) : null;
        }
        return String(j);
    };

    MM.api = function(method, path, body){
        var opts = { method: method, credentials: "same-origin", headers: { "X-CSRFToken": MM.csrf() } };
        if (body !== undefined){
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
        return fetch(MM.apiUrl + path, opts).then(function(r){
            return r.json().catch(function(){ return {}; }).then(function(j){
                if (!r.ok){
                    var e = new Error(MM.errorText(j) || MM.f("The server could not complete the request (status %(status)s). Try again.", {status: r.status}));
                    e.status = r.status;
                    throw e;
                }
                return j;
            });
        }, function(){
            throw new Error(gt("Unable to reach the server. Check the connection and try again."));
        });
    };

    MM.map = function(id){
        var map = L.map(id, { maxZoom: 22 });
        var layers = {};
        var first = null;
        (MM.basemaps || []).forEach(function(b){
            var opts = { attribution: b.attribution, maxZoom: 22, maxNativeZoom: b.maxzoom, minZoom: b.minzoom };
            if (b.subdomains) opts.subdomains = b.subdomains;
            var layer;
            if (b.type === "wms"){
                opts.layers = b.layers;
                opts.format = b.format || "image/png";
                opts.styles = b.styles || "";
                layer = L.tileLayer.wms(b.url, opts);
            }else{
                layer = L.tileLayer(b.url, opts);
            }
            layers[b.label] = layer;
            if (!first) first = layer;
        });
        if (!first){
            first = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "&copy; OpenStreetMap contributors", maxZoom: 22, maxNativeZoom: 19 });
        }
        first.addTo(map);
        if (Object.keys(layers).length > 1) L.control.layers(layers, null, { position: "topright" }).addTo(map);
        L.control.scale({ imperial: false }).addTo(map);
        map.setView([20, 0], 2);
        MM.geocoder(map);
        if (window.ResizeObserver) new ResizeObserver(function(){ map.invalidateSize(); }).observe(map.getContainer());
        MM.fold(map.getContainer());
        MM.fill(map.getContainer());
        return map;
    };

    MM.fill = function(mapEl){
        var app = mapEl.closest(".mm-app");
        var body = app && app.querySelector(".mm-body");
        if (!body) return;
        var apply = function(){
            app.style.setProperty("--mm-top", Math.round(body.getBoundingClientRect().top + window.scrollY) + "px");
        };
        apply();
        window.addEventListener("resize", apply);
    };

    MM.fold = function(mapEl){
        var app = mapEl.closest(".mm-app");
        var body = app && app.querySelector(".mm-body");
        if (!body) return;
        var apply = function(){
            var segs = window.viewport && window.viewport.segments;
            app.classList.remove("mm-folded", "mm-folded-v");
            if (!segs || segs.length !== 2) return;
            var a = segs[0], b = segs[1];
            if (a.right <= b.left){
                var hinge = b.left - a.right, gap = Math.max(24, hinge);
                var col = (a.right + b.left) / 2 - gap / 2 - body.getBoundingClientRect().left;
                app.style.setProperty("--mm-fold-a", Math.max(0, Math.round(col)) + "px");
                app.style.setProperty("--mm-fold-gap", Math.round(gap) + "px");
                app.classList.add("mm-folded");
            }else if (a.bottom <= b.top){
                var h = a.bottom - mapEl.getBoundingClientRect().top - 12;
                app.style.setProperty("--mm-fold-map-h", Math.max(200, Math.round(h)) + "px");
                app.classList.add("mm-folded-v");
            }
        };
        apply();
        window.addEventListener("resize", apply);
    };

    MM.confirm = function(question, detail, yes, no){
        return MM.choose(question, detail, [
            { key: true, label: yes || gt("Yes"), icon: "fa-check", primary: true },
            { key: false, label: no || gt("No"), icon: "fa-times", cancel: true }
        ]);
    };

    MM.choose = function(question, detail, buttons){
        return new Promise(function(resolve){
            var m = document.createElement("div");
            m.className = "modal fade mm-modal";
            m.setAttribute("tabindex", "-1");
            m.setAttribute("role", "dialog");
            m.innerHTML = '<div class="modal-dialog" role="document"><div class="modal-content">' +
                '<div class="modal-body"><p class="mm-question"></p><p class="mm-detail"></p></div>' +
                '<div class="modal-footer"></div></div></div>';
            m.querySelector(".mm-question").textContent = question;
            m.querySelector(".mm-detail").textContent = detail || "";
            var footer = m.querySelector(".modal-footer"), cancel = null, cancelKey = null, answer;
            buttons.forEach(function(b){
                var el = document.createElement("button");
                el.type = "button";
                el.className = "btn " + (b.primary ? "btn-primary" : b.danger ? "btn-danger" : "btn-default");
                el.innerHTML = '<i class="fa ' + b.icon + '" aria-hidden="true"></i> ' + MM.esc(b.label);
                el.onclick = function(){ answer = b.key; $(m).modal("hide"); };
                if (b.cancel){ cancel = el; cancelKey = b.key; }
                footer.appendChild(el);
            });
            answer = cancelKey;
            document.body.appendChild(m);
            $(m).on("shown.bs.modal", function(){ (cancel || footer.lastChild).focus(); });
            $(m).on("hidden.bs.modal", function(){ $(m).remove(); resolve(answer); });
            $(m).modal("show");
        });
    };

    MM.tip = function(text){
        var t = MM.esc(text);
        return '<button type="button" class="mm-info" data-toggle="tooltip" title="' + t + '" aria-label="' + t + '"><i class="fa fa-info-circle" aria-hidden="true"></i></button>';
    };

    MM.tips = function(root){
        if (!$.fn.tooltip){
            window.addEventListener("load", function(){ MM.tips(root); }, { once: true });
            return;
        }
        $(".tooltip").remove();
        $(root || document).find('[data-toggle="tooltip"]').tooltip({ container: "body", trigger: "hover focus", placement: "auto top" });
    };

    MM.upload = function(path, file){
        var fd = new FormData();
        fd.append("file", file);
        return fetch(MM.apiUrl + path, { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": MM.csrf() }, body: fd }).then(function(r){
            return r.json().catch(function(){ return {}; }).then(function(j){
                if (!r.ok) throw new Error(MM.errorText(j) || MM.f("The server could not complete the request (status %(status)s). Try again.", {status: r.status}));
                return j;
            });
        }, function(){
            throw new Error(gt("Unable to reach the server. Check the connection and try again."));
        });
    };

    MM.download = function(path, body){
        return fetch(MM.apiUrl + path, { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": MM.csrf(), "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(function(r){
            if (!r.ok) return r.json().catch(function(){ return {}; }).then(function(j){
                throw new Error(MM.errorText(j) || MM.f("The server could not complete the request (status %(status)s). Try again.", {status: r.status}));
            });
            var cd = r.headers.get("Content-Disposition") || "", m = cd.match(/filename="([^"]+)"/);
            return r.blob().then(function(b){
                var name = m ? m[1] : "mission.kmz";
                var a = document.createElement("a");
                a.href = URL.createObjectURL(b);
                a.download = name;
                document.body.appendChild(a);
                a.click();
                setTimeout(function(){ URL.revokeObjectURL(a.href); a.remove(); }, 1000);
                return name;
            });
        }, function(){
            throw new Error(gt("Unable to reach the server. Check the connection and try again."));
        });
    };

    MM.esc = function(s){
        var d = document.createElement("div");
        d.textContent = s == null ? "" : String(s);
        return d.innerHTML;
    };

    MM.geocoder = function(map){
        var esc = MM.esc, results = [], active = -1, timer = null, ctrl = null, seq = 0;
        var Control = L.Control.extend({
            options: { position: "topright" },
            onAdd: function(){
                var div = L.DomUtil.create("div", "mm-geo");
                div.innerHTML = '<input type="search" class="form-control input-sm" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="mm-geo-list" ' +
                    'aria-label="' + esc(gt("Search for a place")) + '" placeholder="' + esc(gt("Search for a place")) + '" autocomplete="off">' +
                    '<ul class="dropdown-menu dropdown-menu-right" id="mm-geo-list" role="listbox"></ul>';
                L.DomEvent.disableClickPropagation(div);
                L.DomEvent.disableScrollPropagation(div);
                return div;
            }
        });
        var c = new Control();
        c.addTo(map);
        map.attributionControl.addAttribution('<a href="https://photon.komoot.io" target="_blank" rel="noopener">Photon</a>');
        var div = c.getContainer();
        var input = div.querySelector("input");
        var list = div.querySelector("ul");

        var describe = function(p){
            var street = [p.street, p.housenumber].filter(Boolean).join(" ");
            var title = p.name || street || p.city || p.state || p.country || "";
            var parts = [];
            [street, p.district, p.city || p.town || p.village, p.county, p.state, p.country].forEach(function(x){
                if (x && x !== title && parts.indexOf(x) === -1) parts.push(x);
            });
            return { title: title, detail: parts.join(", ") };
        };

        var fit = function(){
            var w = Math.max(140, Math.min(300, map.getSize().x - 150));
            input.style.width = w + "px";
            list.style.width = w + "px";
            var layers = map.getContainer().querySelector(".leaflet-control-layers");
            if (layers && layers.offsetHeight) input.style.height = layers.offsetHeight + "px";
            var room = map.getContainer().getBoundingClientRect().bottom - input.getBoundingClientRect().bottom - 30;
            list.style.maxHeight = Math.max(80, room) + "px";
        };
        map.on("resize", fit);

        var show = function(items, note){
            results = items;
            active = -1;
            if (!items.length && !note){
                list.style.display = "none";
                input.setAttribute("aria-expanded", "false");
                return;
            }
            list.innerHTML = items.length ? items.map(function(r, i){
                return '<li role="option" id="mm-geo-' + i + '"><a href="#" tabindex="-1" data-i="' + i + '"><span class="mm-geo-title">' + esc(r.title) + '</span>' +
                    (r.detail ? '<span class="mm-geo-detail text-muted">' + esc(r.detail) + '</span>' : '') + '</a></li>';
            }).join("") : '<li class="disabled"><a href="#" tabindex="-1"><span class="mm-geo-detail">' + esc(note) + '</span></a></li>';
            list.style.display = "block";
            fit();
            input.setAttribute("aria-expanded", "true");
            Array.prototype.forEach.call(list.querySelectorAll("[data-i]"), function(a){
                a.onmousedown = function(e){ e.preventDefault(); };
                a.onclick = function(e){ e.preventDefault(); go(parseInt(a.getAttribute("data-i"))); };
            });
        };

        var highlight = function(i){
            active = i;
            Array.prototype.forEach.call(list.children, function(li, j){
                var a = li.querySelector("a");
                if (a) a.className = j === i ? "theme-background-highlight" : "";
                if (j === i) li.scrollIntoView({ block: "nearest" });
            });
            if (i >= 0) input.setAttribute("aria-activedescendant", "mm-geo-" + i);
            else input.removeAttribute("aria-activedescendant");
        };

        var go = function(i){
            var r = results[i];
            if (!r) return;
            if (r.bounds) map.fitBounds(r.bounds, { maxZoom: 18 });
            else map.setView(r.latlng, 17);
            input.value = r.title;
            show([]);
        };

        var search = function(q){
            clearTimeout(timer);
            if (ctrl) ctrl.abort();
            var coords = q.match(/^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$/);
            if (coords){
                var lat = parseFloat(coords[1]), lon = parseFloat(coords[2]);
                if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180){
                    show([{ title: MM.f("Go to %(lat)s, %(lon)s", {lat: lat, lon: lon}), detail: "", latlng: [lat, lon] }]);
                    return;
                }
            }
            if (q.length < 3){ show([]); return; }
            timer = setTimeout(function(){
                var s = ++seq, center = map.getCenter();
                ctrl = window.AbortController ? new AbortController() : null;
                var url = "https://photon.komoot.io/api/?limit=8&q=" + encodeURIComponent(q);
                if (map.getZoom() >= 5) url += "&lat=" + center.lat.toFixed(4) + "&lon=" + center.lng.toFixed(4);
                fetch(url, ctrl ? { signal: ctrl.signal } : {}).then(function(r){
                    if (!r.ok) throw new Error();
                    return r.json();
                }).then(function(j){
                    if (s !== seq) return;
                    var seen = {}, items = [];
                    (j.features || []).forEach(function(f){
                        var d = describe(f.properties), key = d.title + "|" + d.detail, e = f.properties.extent;
                        if (!d.title || seen[key]) return;
                        seen[key] = true;
                        items.push({
                            title: d.title,
                            detail: d.detail,
                            latlng: [f.geometry.coordinates[1], f.geometry.coordinates[0]],
                            bounds: e ? [[e[3], e[0]], [e[1], e[2]]] : null
                        });
                    });
                    show(items, items.length ? null : gt("No places found. Try a different spelling or a nearby town."));
                }).catch(function(e){
                    if (s !== seq || (e && e.name === "AbortError")) return;
                    show([], gt("Unable to search places right now. Check the connection and try again."));
                });
            }, 350);
        };

        fit();
        input.addEventListener("input", function(){ search(input.value.trim()); });
        input.addEventListener("keydown", function(e){
            e.stopPropagation();
            if (e.key === "ArrowDown" && results.length){ e.preventDefault(); highlight((active + 1) % results.length); }
            else if (e.key === "ArrowUp" && results.length){ e.preventDefault(); highlight((active - 1 + results.length) % results.length); }
            else if (e.key === "Enter"){ e.preventDefault(); go(active >= 0 ? active : 0); }
            else if (e.key === "Escape"){ show([]); }
        });
        input.addEventListener("blur", function(){ setTimeout(function(){ show([]); }, 150); });
        input.addEventListener("focus", function(){ if (results.length) show(results); });
        return c;
    };
})();
