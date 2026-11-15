# Mission Planner

A [WebODM](https://github.com/WebODM/WebODM) plugin for planning DJI drone missions and sending them to DJI Fly.

## Features

- Five mission types: 2D Mapping, Oblique Mapping, Corridor Mapping, Orbit (3D Model) and Panorama.
- Draw the area, line, circle or points on the map, or import a GeoJSON, KML or KMZ file.
- Live route preview with flight time, distance, photos, waypoints and batteries.
- Semi or full-automatic capture, curved or straight flight paths, automatic flight direction, cross-hatch and reverse routes.
- Terrain follow with online elevation data, and takeoff zones.
- Splits long missions into flights by battery and waypoint limits, with manual breaks for corridors.
- Download DJI Fly missions (KMZ), or replace a mission directly on a DJI RC connected over USB.
- Missions are saved in WebODM projects: people who can see a project can open its missions, people who can edit it can change them.

## Install

Download the `.zip` from the [releases page](../../releases), then in WebODM go to **Administration** → **Plugins** → **Load Plugin (.zip)**.

## Usage

Open **Mission Planner** from the menu and press **New mission**. Pick the mission type, draw the area on the map and adjust the flight settings: the route updates as you go. Save it to a project, then download the KMZ from the **Export** tab.

Sending a mission straight to a DJI RC over USB works when WebODM runs natively on Windows and the browser is on the same computer. Otherwise, download the flight for the controller and copy it over by following the steps shown.

## Contributing

Feedback is welcome! Open an [issue](../../issues) to report bugs, or suggest features. Pull requests are welcome too.

## License

[AGPLv3](LICENSE). The planning engine in `flypath_engine` is Apache 2.0.

## Credits

Originally based on [FlyPath](https://github.com/dronnix-io/FlyPath) and the [FlyPath engine](https://pypi.org/project/flypath-engine/) by [Dronnix](https://www.dronnix.com).
