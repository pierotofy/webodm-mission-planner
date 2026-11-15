import io
import time
import zipfile
from dataclasses import dataclass


@dataclass
class MissionSpec:
    waypoints: list
    altitude_m: float
    speed_ms: float
    finish_action: str
    rc_lost_action: str
    gimbal_pitch: float = -90.0
    mission_name: str = 'Mission'
    create_time_ms: int = None
    capture_mode: str = 'semi'
    actions: list = None
    heights: list = None
    curved_path: bool = True
    headings: list = None
    gimbal_pitches: list = None


def esc(text):
    return (text.replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;'))


_FINISH_ACTION = {
    'Return to Home':         'goHome',
    'Hover in place':         'hover',
    'Land at last waypoint':  'autoLand',
}

_RC_LOST_ACTION = {
    'Return to Home':   ('executeLostAction', 'goBack'),
    'Hover in place':   ('executeLostAction', 'hover'),
    'Land immediately': ('executeLostAction', 'landing'),
    'Continue mission': ('goContinue',        'goBack'),
}

_NS = 'http://www.uav.com/wpmz/1.0.2'

_GIMBAL_SETTLE_S = 3.0

_STRAIGHT_TURN_MODE = 'toPointAndStopWithDiscontinuityCurvature'
_CURVED_TURN_MODE   = 'toPointAndPassWithContinuityCurvature'


def write(drone_enum, spec):
    if not spec.waypoints:
        raise ValueError('No waypoints provided — define a survey area first.')

    finish_action = _FINISH_ACTION.get(spec.finish_action, 'goHome')
    height_mode   = 'relativeToStartPoint'
    exit_on_rc_lost, rc_lost_action = _RC_LOST_ACTION.get(
        spec.rc_lost_action, ('executeLostAction', 'goBack')
    )
    ts_ms = int(spec.create_time_ms) if spec.create_time_ms else int(time.time() * 1000)

    if spec.heights is not None and len(spec.heights) != len(spec.waypoints):
        raise ValueError('Terrain heights do not match the waypoints.')
    if spec.headings is not None and len(spec.headings) != len(spec.waypoints):
        raise ValueError('Waypoint headings do not match the waypoints.')
    if spec.gimbal_pitches is not None and len(spec.gimbal_pitches) != len(spec.waypoints):
        raise ValueError('Waypoint camera pitches do not match the waypoints.')

    mission_config = _mission_config_xml(drone_enum, finish_action,
                                         spec.speed_ms, exit_on_rc_lost, rc_lost_action)
    placemarks = _placemark_blocks(spec.waypoints, spec.altitude_m, spec.speed_ms,
                                   spec.gimbal_pitch, spec.capture_mode, spec.heights,
                                   spec.curved_path, spec.actions, spec.headings,
                                   spec.gimbal_pitches)
    template_kml   = _build_template_kml(mission_config, ts_ms, spec.mission_name,
                                         spec.speed_ms, spec.altitude_m, height_mode,
                                         placemarks)
    waylines_wpml  = _build_waylines_wpml(mission_config, spec.speed_ms, height_mode,
                                          placemarks)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('wpmz/template.kml', template_kml)
        zf.writestr('wpmz/waylines.wpml', waylines_wpml)
    return buf.getvalue()



def _mission_config_xml(drone_enum, finish_action, speed_ms,
                        exit_on_rc_lost, rc_lost_action):
    transitional_speed = min(speed_ms, 5.0)
    return f'''    <wpml:missionConfig>
      <wpml:flyToWaylineMode>safely</wpml:flyToWaylineMode>
      <wpml:finishAction>{finish_action}</wpml:finishAction>
      <wpml:exitOnRCLost>{exit_on_rc_lost}</wpml:exitOnRCLost>
      <wpml:executeRCLostAction>{rc_lost_action}</wpml:executeRCLostAction>
      <wpml:globalTransitionalSpeed>{transitional_speed:.1f}</wpml:globalTransitionalSpeed>
      <wpml:droneInfo>
        <wpml:droneEnumValue>{drone_enum}</wpml:droneEnumValue>
        <wpml:droneSubEnumValue>0</wpml:droneSubEnumValue>
      </wpml:droneInfo>
    </wpml:missionConfig>'''



def _build_template_kml(mission_config, ts_ms, mission_name,
                        speed_ms, altitude_m, height_mode, placemarks):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"
     xmlns:wpml="{_NS}">
  <Document>
    <wpml:author>{esc(mission_name)}</wpml:author>
    <wpml:createTime>{ts_ms}</wpml:createTime>
    <wpml:updateTime>{ts_ms}</wpml:updateTime>
{mission_config}
    <Folder>
      <wpml:templateType>waypoint</wpml:templateType>
      <wpml:templateId>0</wpml:templateId>
      <wpml:waylineCoordinateSysParam>
        <wpml:coordinateMode>WGS84</wpml:coordinateMode>
        <wpml:heightMode>{height_mode}</wpml:heightMode>
        <wpml:positioningType>GPS</wpml:positioningType>
      </wpml:waylineCoordinateSysParam>
      <wpml:autoFlightSpeed>{speed_ms:.1f}</wpml:autoFlightSpeed>
      <wpml:globalHeight>{altitude_m:.1f}</wpml:globalHeight>
      <wpml:caliFlightEnable>0</wpml:caliFlightEnable>
      <wpml:gimbalPitchMode>usePointSetting</wpml:gimbalPitchMode>
{placemarks}
    </Folder>
  </Document>
</kml>
'''


def _placemark_blocks(waypoints, altitude_m, speed_ms, gimbal_pitch,
                      capture_mode='semi', heights=None, curved=True, actions=None,
                      headings=None, gimbal_pitches=None):
    placemark_blocks = []
    group_id = 1
    full = capture_mode == 'full'
    planned = None
    if actions is not None:
        planned = {index: [] for index in range(len(waypoints))}
        for action in actions:
            index = action.get('waypoint_index') if isinstance(action, dict) else None
            if type(index) is not int or index not in planned:
                raise ValueError('Planned action does not match the flight waypoints.')
            planned[index].append(action)

    for idx, (lon, lat) in enumerate(waypoints):
        if planned is not None:
            waypoint_actions = planned[idx]
        else:
            waypoint_actions = [{'type': 'rotate_camera', 'pitch_deg': gimbal_pitch}] if idx == 0 else []
            if full:
                waypoint_actions.append({'type': 'take_photo'})
        action_groups = ''
        if waypoint_actions:
            action_groups = _planned_action_group(
                group_id, idx, waypoint_actions, gimbal_pitch, legacy=planned is None)
            group_id += 1
        wp_height = heights[idx] if heights is not None else altitude_m
        heading = headings[idx] if headings is not None else None
        pitch = gimbal_pitch
        if gimbal_pitches is not None:
            pitch = gimbal_pitches[idx]
            pitch = int(pitch) if float(pitch).is_integer() else pitch
        placemark_blocks.append(
            _placemark(idx, lon, lat, wp_height, speed_ms,
                       action_groups, pitch, curved, heading)
        )

    return '\n'.join(placemark_blocks)


def _planned_action_group(group_id, index, actions, gimbal_pitch, *, legacy=False):
    sequence = not legacy or len(actions) > 1
    first_id = 0 if sequence else group_id
    blocks = []
    for action_id, action in enumerate(actions, start=first_id):
        kind = action.get('type')
        if kind == 'rotate_camera':
            pitch = action.get('pitch_deg', gimbal_pitch)
            timed = legacy and sequence
            roll = '<wpml:gimbalRollRotateAngle>0</wpml:gimbalRollRotateAngle>' if legacy else ''
            yaw = '<wpml:gimbalYawRotateAngle>0</wpml:gimbalYawRotateAngle>' if legacy else ''
            duration = f'{_GIMBAL_SETTLE_S:.1f}' if timed else '0'
            rotation_time = f'<wpml:gimbalRotateTime>{duration}</wpml:gimbalRotateTime>' if legacy else ''
            body = f'''            <wpml:actionActuatorFunc>gimbalRotate</wpml:actionActuatorFunc>
            <wpml:actionActuatorFuncParam>
              <wpml:gimbalHeadingYawBase>aircraft</wpml:gimbalHeadingYawBase>
              <wpml:gimbalRotateMode>absoluteAngle</wpml:gimbalRotateMode>
              <wpml:gimbalPitchRotateEnable>1</wpml:gimbalPitchRotateEnable>
              <wpml:gimbalPitchRotateAngle>{pitch}</wpml:gimbalPitchRotateAngle>
              <wpml:gimbalRollRotateEnable>0</wpml:gimbalRollRotateEnable>{roll}
              <wpml:gimbalYawRotateEnable>0</wpml:gimbalYawRotateEnable>{yaw}
              <wpml:gimbalRotateTimeEnable>{int(timed)}</wpml:gimbalRotateTimeEnable>{rotation_time}
              <wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>
            </wpml:actionActuatorFuncParam>'''
        elif kind == 'hover':
            body = f'''            <wpml:actionActuatorFunc>hover</wpml:actionActuatorFunc>
            <wpml:actionActuatorFuncParam>
              <wpml:hoverTime>{float(action['duration_s']):.1f}</wpml:hoverTime>
            </wpml:actionActuatorFuncParam>'''
        elif kind == 'take_photo':
            body = '''            <wpml:actionActuatorFunc>takePhoto</wpml:actionActuatorFunc>
            <wpml:actionActuatorFuncParam>
              <wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>
            </wpml:actionActuatorFuncParam>'''
        else:
            raise ValueError('Unsupported planned action: %s.' % kind)
        blocks.append(f'''          <wpml:action>
            <wpml:actionId>{action_id}</wpml:actionId>
{body}
          </wpml:action>''')
    mode = 'sequence' if sequence else 'parallel'
    return f'''        <wpml:actionGroup>
          <wpml:actionGroupId>{group_id}</wpml:actionGroupId>
          <wpml:actionGroupStartIndex>{index}</wpml:actionGroupStartIndex>
          <wpml:actionGroupEndIndex>{index}</wpml:actionGroupEndIndex>
          <wpml:actionGroupMode>{mode}</wpml:actionGroupMode>
          <wpml:actionTrigger>
            <wpml:actionTriggerType>reachPoint</wpml:actionTriggerType>
          </wpml:actionTrigger>
{chr(10).join(blocks)}
        </wpml:actionGroup>
'''


def _build_waylines_wpml(mission_config, speed_ms, height_mode, placemarks):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"
     xmlns:wpml="{_NS}">
  <Document>
{mission_config}
    <Folder>
      <wpml:templateId>0</wpml:templateId>
      <wpml:executeHeightMode>{height_mode}</wpml:executeHeightMode>
      <wpml:waylineId>0</wpml:waylineId>
      <wpml:distance>0</wpml:distance>
      <wpml:duration>0</wpml:duration>
      <wpml:autoFlightSpeed>{speed_ms:.1f}</wpml:autoFlightSpeed>
{placemarks}
    </Folder>
  </Document>
</kml>
'''



def _placemark(idx, lon, lat, altitude_m, speed_ms, action_groups_xml,
               gimbal_pitch=-90, curved=True, heading=None):
    turn_mode = _CURVED_TURN_MODE if curved else _STRAIGHT_TURN_MODE
    use_straight_line = 0 if curved else 1
    heading_mode, heading_angle, heading_enable = (
        ('followWayline', '0', 0) if heading is None
        else ('smoothTransition', f'{float(heading):.1f}', 1))
    return f'''      <Placemark>
        <Point>
          <coordinates>
            {lon:.8f},{lat:.8f}
          </coordinates>
        </Point>
        <wpml:index>{idx}</wpml:index>
        <wpml:executeHeight>{altitude_m:.1f}</wpml:executeHeight>
        <wpml:waypointSpeed>{speed_ms:.1f}</wpml:waypointSpeed>
        <wpml:waypointHeadingParam>
          <wpml:waypointHeadingMode>{heading_mode}</wpml:waypointHeadingMode>
          <wpml:waypointHeadingAngle>{heading_angle}</wpml:waypointHeadingAngle>
          <wpml:waypointPoiPoint>0.000000,0.000000,0.000000</wpml:waypointPoiPoint>
          <wpml:waypointHeadingAngleEnable>{heading_enable}</wpml:waypointHeadingAngleEnable>
          <wpml:waypointHeadingPathMode>followBadArc</wpml:waypointHeadingPathMode>
          <wpml:waypointHeadingPoiIndex>0</wpml:waypointHeadingPoiIndex>
        </wpml:waypointHeadingParam>
        <wpml:waypointTurnParam>
          <wpml:waypointTurnMode>{turn_mode}</wpml:waypointTurnMode>
          <wpml:waypointTurnDampingDist>0</wpml:waypointTurnDampingDist>
        </wpml:waypointTurnParam>
        <wpml:useStraightLine>{use_straight_line}</wpml:useStraightLine>
{action_groups_xml}        <wpml:waypointGimbalHeadingParam>
          <wpml:waypointGimbalPitchAngle>{gimbal_pitch}</wpml:waypointGimbalPitchAngle>
          <wpml:waypointGimbalYawAngle>0</wpml:waypointGimbalYawAngle>
        </wpml:waypointGimbalHeadingParam>
      </Placemark>'''
