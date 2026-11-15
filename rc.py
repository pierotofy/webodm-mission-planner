import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile


_RC_EXIT_FOUND = 0
_RC_EXIT_NONE = 10
_RC_EXIT_DEVICE_NO_WP = 11
_RC_REL_PARTS = ['Android', 'data', 'dji.go.v5', 'files', 'waypoint']
_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
    r'-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
try:
    _STARTUPINFO = subprocess.STARTUPINFO()
    _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _STARTUPINFO.wShowWindow = 0
except Exception:
    _STARTUPINFO = None


def find_waypoint_on_drives():
    rel = os.path.join(*_RC_REL_PARTS)
    roots = []
    if sys.platform == 'win32':
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            bitmask = k32.GetLogicalDrives()
            for index in range(26):
                if not (bitmask >> index) & 1:
                    continue
                root = chr(ord('A') + index) + ':\\'
                if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) in (2, 3):
                    roots.append(root)
        except Exception:
            import string
            roots = [letter + ':\\' for letter in string.ascii_uppercase
                     if os.path.isdir(letter + ':\\')]
    elif sys.platform == 'linux':
        gvfs = os.path.join(
            os.getenv('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'), 'gvfs')
        try:
            for name in os.listdir(gvfs):
                if not name.startswith('mtp:'):
                    continue
                device = os.path.join(gvfs, name)
                try:
                    roots.extend(os.path.join(device, child)
                                 for child in os.listdir(device))
                except OSError:
                    continue
        except OSError:
            pass
    for root in roots:
        candidate = os.path.join(root, rel)
        try:
            if os.path.isdir(candidate):
                return candidate
        except (OSError, ValueError):
            continue
    return None


def list_missions_from_dir(waypoint_dir):
    try:
        if not os.path.isdir(waypoint_dir):
            return 'error', []
        preview_dir = os.path.join(waypoint_dir, 'map_preview')
        has_preview = os.path.isdir(preview_dir)
        preview = ({name for name in os.listdir(preview_dir)
                    if os.path.isdir(os.path.join(preview_dir, name))}
                   if has_preview else set())
        missions = []
        for uuid in os.listdir(waypoint_dir):
            folder = os.path.join(waypoint_dir, uuid)
            if not (os.path.isdir(folder) and _UUID_RE.match(uuid)):
                continue
            if has_preview and uuid not in preview:
                continue
            kmz = os.path.join(folder, uuid + '.kmz')
            metadata = _read_kmz_meta(kmz) if os.path.exists(kmz) else (0, 0, [])
            missions.append(_mission(uuid, *metadata))
        missions.sort(key=lambda item: item['create_ms'] or 0, reverse=True)
        return ('ok' if missions else 'no_mission'), missions
    except Exception:
        return 'error', []


def _mission(uuid, create_ms, waypoint_count, waypoints):
    return {
        'uuid': uuid,
        'create_ms': create_ms,
        'date_str': _format_time(create_ms),
        'n_wp': waypoint_count,
        'waypoints': waypoints,
    }


def _read_kmz_meta(kmz_path):
    try:
        with zipfile.ZipFile(kmz_path) as archive:
            template = archive.read('wpmz/template.kml').decode('utf-8', 'replace')
            try:
                waylines = archive.read('wpmz/waylines.wpml').decode('utf-8', 'replace')
            except KeyError:
                waylines = ''
        match = re.search(r'<wpml:createTime>(\d+)</wpml:createTime>', template)
        create_ms = int(match.group(1)) if match else 0
        waypoint_count = len(re.findall(r'<wpml:index>', waylines))
        waypoints = [
            (float(longitude), float(latitude))
            for longitude, latitude in re.findall(
                r'<coordinates>\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)', waylines)
        ]
        return create_ms, waypoint_count, waypoints
    except Exception:
        return 0, 0, []


def _format_time(create_ms):
    if not create_ms:
        return 'unknown date'
    try:
        return datetime.datetime.fromtimestamp(
            create_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return 'unknown date'


def _powershell():
    return os.path.join(
        os.environ.get('SystemRoot', r'C:\Windows'),
        r'System32\WindowsPowerShell\v1.0\powershell.exe')


def _run(script, *, timeout):
    temp_dir = tempfile.mkdtemp(prefix='mission_planner_')
    path = os.path.join(temp_dir, 'controller.ps1')
    try:
        with open(path, 'w', encoding='utf-8') as output:
            output.write(script(temp_dir))
        result = subprocess.run(
            [_powershell(), '-NoProfile', '-NonInteractive', '-STA',
             '-ExecutionPolicy', 'Bypass', '-File', path],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW, startupinfo=_STARTUPINFO)
        return result, temp_dir
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def list_shell_children(parts):
    if sys.platform != 'win32':
        if not parts:
            roots = ['/\0System Root', os.path.expanduser('~') + '\0Home']
            gvfs = os.path.join(
                os.getenv('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'), 'gvfs')
            try:
                roots.extend(os.path.join(gvfs, name) + '\0' + name
                             for name in os.listdir(gvfs))
            except OSError:
                pass
            return roots
        path = os.path.join(*parts)
        try:
            return [name for name in sorted(os.listdir(path))
                    if os.path.isdir(os.path.join(path, name))]
        except OSError:
            return []
    try:
        result, temp_dir = _run(
            lambda _temp: _shell_children_script(parts), timeout=40)
        return [line[2:] for line in result.stdout.splitlines()
                if line.startswith('D|')]
    except Exception:
        return []
    finally:
        if 'temp_dir' in locals():
            shutil.rmtree(temp_dir, ignore_errors=True)


def _shell_children_script(parts):
    escaped = ', '.join("'" + part.replace("'", "''") + "'" for part in parts)
    return (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        '$shell = New-Object -ComObject Shell.Application\n'
        "$folder = $shell.Namespace('::{20D04FE0-3AEA-1069-A2D8-08002B30309D}')\n"
        '$parts = @(' + escaped + ')\n'
        'foreach ($p in $parts) {\n'
        '    $hit = $null\n'
        '    foreach ($i in $folder.Items()) { if ($i.Name -eq $p) { $hit = $i; break } }\n'
        '    if (-not $hit) { exit 1 }\n'
        '    $folder = $hit.GetFolder\n'
        '    if (-not $folder) { exit 1 }\n'
        '}\n'
        'foreach ($i in $folder.Items()) {\n'
        '    if ($i.IsFolder) { Write-Output ("D|" + $i.Name) }\n'
        '}\n'
    )


def list_missions_at_path(parts):
    if sys.platform != 'win32':
        return list_missions_from_dir(os.path.join(*parts))
    try:
        result, temp_dir = _run(
            lambda temp: _missions_at_path_script(parts, temp), timeout=120)
        if result.returncode != 0:
            return 'error', []
        uuids = [line[len('UUID='):].strip()
                 for line in result.stdout.splitlines() if line.startswith('UUID=')]
        missions = [_mission(uuid, *_read_kmz_meta(
            os.path.join(temp_dir, uuid + '.kmz'))) for uuid in uuids]
        missions.sort(key=lambda item: item['create_ms'] or 0, reverse=True)
        return ('ok' if missions else 'no_mission'), missions
    except Exception:
        return 'error', []
    finally:
        if 'temp_dir' in locals():
            shutil.rmtree(temp_dir, ignore_errors=True)


def _missions_at_path_script(parts, temp_dir):
    escaped = ', '.join("'" + part.replace("'", "''") + "'" for part in parts)
    destination = temp_dir.replace("'", "''")
    return (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        '$shell = New-Object -ComObject Shell.Application\n'
        "$folder = $shell.Namespace('::{20D04FE0-3AEA-1069-A2D8-08002B30309D}')\n"
        '$uuidPattern = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
        '[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"\n'
        "$dest = $shell.Namespace('" + destination + "')\n"
        '$parts = @(' + escaped + ')\n'
        'foreach ($p in $parts) {\n'
        '    $hit = $null\n'
        '    foreach ($i in $folder.Items()) { if ($i.Name -eq $p) { $hit = $i; break } }\n'
        '    if (-not $hit) { exit 1 }\n'
        '    $folder = $hit.GetFolder\n'
        '    if (-not $folder) { exit 1 }\n'
        '}\n'
        '$wp = $folder\n'
        '$preview = @{}; $hasPreview = $false\n'
        'foreach ($c in $wp.Items()) {\n'
        "    if ($c.IsFolder -and $c.Name -eq 'map_preview') {\n"
        '        $mpf = $c.GetFolder\n'
        '        if ($mpf) { $hasPreview = $true; foreach ($pv in $mpf.Items()) { if ($pv.IsFolder) { $preview[$pv.Name] = $true } } }\n'
        '    }\n'
        '}\n'
        'foreach ($item in $wp.Items()) {\n'
        '    if ($item.IsFolder -and $item.Name -match $uuidPattern) {\n'
        '        if ($hasPreview -and -not $preview.ContainsKey($item.Name)) { continue }\n'
        "        Write-Output ('UUID=' + $item.Name)\n"
        '        $mf = $item.GetFolder\n'
        '        foreach ($f in $mf.Items()) {\n'
        '            if (-not $f.IsFolder) { $dest.CopyHere($f, 0x10); Start-Sleep -Milliseconds 1500 }\n'
        '        }\n'
        '    }\n'
        '}\n'
        'exit 0\n'
    )


def list_rc_missions():
    if sys.platform != 'win32':
        return 'not_connected', None, [], ''
    try:
        result, temp_dir = _run(_rc_list_script, timeout=120)
    except subprocess.TimeoutExpired:
        return 'error', None, [], 'Timed out while reading the RC.'
    except Exception as exc:
        return 'error', None, [], f'PowerShell error: {exc}'
    try:
        if result.returncode == _RC_EXIT_DEVICE_NO_WP:
            return 'no_mission', None, [], ''
        if result.returncode == _RC_EXIT_NONE:
            return 'not_connected', None, [], ''
        if result.returncode != _RC_EXIT_FOUND:
            return ('error', None, [],
                    result.stderr.strip() or f'Scan failed (exit {result.returncode}).')
        waypoint_path = None
        uuids = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith('PATH='):
                waypoint_path = line[len('PATH='):]
            elif line.startswith('UUID='):
                uuids.append(line[len('UUID='):])
        missions = [_mission(uuid, *_read_kmz_meta(
            os.path.join(temp_dir, uuid + '.kmz'))) for uuid in uuids]
        missions.sort(key=lambda item: item['create_ms'] or 0, reverse=True)
        if not missions:
            return 'no_mission', waypoint_path, [], ''
        return 'ok', waypoint_path, missions, ''
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _rc_list_script(temp_dir):
    relative = ', '.join("'" + part + "'" for part in _RC_REL_PARTS)
    joined = '\\'.join(_RC_REL_PARTS)
    destination = temp_dir.replace("'", "''")
    return (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        '$shell = New-Object -ComObject Shell.Application\n'
        "$thisPC = $shell.Namespace('::{20D04FE0-3AEA-1069-A2D8-08002B30309D}')\n"
        '$rel = @(' + relative + ')\n'
        '$uuidPattern = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
        '[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"\n'
        "$dest = $shell.Namespace('" + destination + "')\n"
        '$deviceSeen = $false\n'
        'function Nav($folder, $parts) {\n'
        '    foreach ($part in $parts) {\n'
        '        $hit = $null\n'
        '        foreach ($item in $folder.Items()) {\n'
        '            if ($item.Name -eq $part) { $hit = $item; break }\n'
        '        }\n'
        '        if (-not $hit) { return $null }\n'
        '        $nf = $hit.GetFolder\n'
        '        if (-not $nf) { return $null }\n'
        '        $folder = $nf\n'
        '    }\n'
        '    return $folder\n'
        '}\n'
        'foreach ($device in $thisPC.Items()) {\n'
        '    if (-not $device.IsFolder) { continue }\n'
        "    if ($device.Path -match '^[A-Za-z]:\\\\?$') { continue }\n"
        '    $devFolder = $device.GetFolder\n'
        '    if (-not $devFolder) { continue }\n'
        "    if ($device.Name -match 'DJI|RC') { $deviceSeen = $true }\n"
        '    $roots = New-Object System.Collections.ArrayList\n'
        '    [void]$roots.Add(@($device.Name, $devFolder))\n'
        '    foreach ($vol in $devFolder.Items()) {\n'
        '        if ($vol.IsFolder) {\n'
        '            $vf = $vol.GetFolder\n'
        '            if ($vf) { [void]$roots.Add(@(($device.Name + "\\" + $vol.Name), $vf)) }\n'
        '        }\n'
        '    }\n'
        '    foreach ($root in $roots) {\n'
        '        $wp = Nav $root[1] $rel\n'
        '        if ($wp) {\n'
        '            $deviceSeen = $true\n'
        "            Write-Output ('PATH=' + $root[0] + '\\" + joined + "')\n"
        '            $preview = @{}\n'
        '            $hasPreviewDir = $false\n'
        '            $mp = $null\n'
        "            foreach ($c in $wp.Items()) { if ($c.IsFolder -and $c.Name -eq 'map_preview') { $mp = $c; break } }\n"
        '            if ($mp) {\n'
        '                $mpf = $mp.GetFolder\n'
        '                if ($mpf) {\n'
        '                    $hasPreviewDir = $true\n'
        '                    foreach ($pv in $mpf.Items()) { if ($pv.IsFolder) { $preview[$pv.Name] = $true } }\n'
        '                }\n'
        '            }\n'
        '            foreach ($item in $wp.Items()) {\n'
        '                if ($item.IsFolder -and $item.Name -match $uuidPattern) {\n'
        '                    if ($hasPreviewDir -and -not $preview.ContainsKey($item.Name)) { continue }\n'
        "                    Write-Output ('UUID=' + $item.Name)\n"
        '                    $mf = $item.GetFolder\n'
        '                    foreach ($f in $mf.Items()) {\n'
        '                        if (-not $f.IsFolder) {\n'
        '                            $dest.CopyHere($f, 0x10)\n'
        '                            Start-Sleep -Milliseconds 1500\n'
        '                        }\n'
        '                    }\n'
        '                }\n'
        '            }\n'
        f'            exit {_RC_EXIT_FOUND}\n'
        '        }\n'
        '    }\n'
        '}\n'
        f'if ($deviceSeen) {{ exit {_RC_EXIT_DEVICE_NO_WP} }}\n'
        f'exit {_RC_EXIT_NONE}\n'
    )


_MTP_EXIT_NAV_FAIL = 1
_MTP_EXIT_UUID_MISSING = 3
_MTP_EXIT_UUID_NO_OPEN = 4

_IFILEOP_CS = r'''using System;
using System.Runtime.InteropServices;
namespace MissionPlannerIFO {
  [ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IShellItem {
    void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
    void GetParent(out IShellItem ppsi);
    void GetDisplayName(uint sigdnName, out IntPtr ppszName);
    void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
    void Compare(IShellItem psi, uint hint, out int piOrder);
  }
  [ComImport, Guid("947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IFileOperation {
    uint Advise(IntPtr pfops, out uint pdwCookie);
    void Unadvise(uint dwCookie);
    void SetOperationFlags(uint dwOperationFlags);
    void SetProgressMessage([MarshalAs(UnmanagedType.LPWStr)] string pszMessage);
    void SetProgressDialog(IntPtr popd);
    void SetProperties(IntPtr pproparray);
    void SetOwnerWindow(IntPtr hwndOwner);
    void ApplyPropertiesToItem(IShellItem psiItem);
    void ApplyPropertiesToItems(IntPtr punkItems);
    void RenameItem(IShellItem psiItem, [MarshalAs(UnmanagedType.LPWStr)] string pszNewName, IntPtr pfopsItem);
    void RenameItems(IntPtr pUnkItems, [MarshalAs(UnmanagedType.LPWStr)] string pszNewName);
    void MoveItem(IShellItem psiItem, IShellItem psiDestinationFolder, [MarshalAs(UnmanagedType.LPWStr)] string pszNewName, IntPtr pfopsItem);
    void MoveItems(IntPtr punkItems, IShellItem psiDestinationFolder);
    void CopyItem(IShellItem psiItem, IShellItem psiDestinationFolder, [MarshalAs(UnmanagedType.LPWStr)] string pszCopyName, IntPtr pfopsItem);
    void CopyItems(IntPtr punkItems, IShellItem psiDestinationFolder);
    void DeleteItem(IShellItem psiItem, IntPtr pfopsItem);
    void DeleteItems(IntPtr punkItems);
    void NewItem(IShellItem psiDestinationFolder, uint dwFileAttributes, [MarshalAs(UnmanagedType.LPWStr)] string pszName, [MarshalAs(UnmanagedType.LPWStr)] string pszTemplateName, IntPtr pfopsItem);
    void PerformOperations();
    void GetAnyOperationsAborted([MarshalAs(UnmanagedType.Bool)] out bool pfAnyOperationsAborted);
  }
  public static class Op {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHCreateItemFromParsingName([MarshalAs(UnmanagedType.LPWStr)] string pszPath, IntPtr pbc, [In] ref Guid riid, [MarshalAs(UnmanagedType.Interface)] out IShellItem ppv);
    [DllImport("shell32.dll", PreserveSig = false)]
    static extern void SHGetIDListFromObject([MarshalAs(UnmanagedType.IUnknown)] object punk, out IntPtr ppidl);
    [DllImport("shell32.dll", PreserveSig = false)]
    static extern void SHCreateItemFromIDList(IntPtr pidl, [In] ref Guid riid, [MarshalAs(UnmanagedType.Interface)] out IShellItem ppv);
    static Guid IID = new Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe");
    static Guid CLSID = new Guid("3ad05575-8857-4850-9277-11b85bdb8e09");
    static IFileOperation New() {
      var op = (IFileOperation)Activator.CreateInstance(Type.GetTypeFromCLSID(CLSID));
      op.SetOperationFlags(1556);
      return op;
    }
    static IShellItem FromObject(object o) {
      IntPtr pidl; SHGetIDListFromObject(o, out pidl);
      Guid g = IID; IShellItem si; SHCreateItemFromIDList(pidl, ref g, out si);
      Marshal.FreeCoTaskMem(pidl);
      return si;
    }
    public static string CopyTo(string srcFile, object destFolderObj, string copyName) {
      string step = "src";
      try {
        Guid a = IID; IShellItem src; SHCreateItemFromParsingName(srcFile, IntPtr.Zero, ref a, out src);
        step = "dst"; IShellItem dst = FromObject(destFolderObj);
        step = "copy"; var op = New(); op.CopyItem(src, dst, copyName, IntPtr.Zero); op.PerformOperations();
        Marshal.ReleaseComObject(op);
        return "OK";
      } catch (Exception e) { return "ERR@" + step + ": " + e.Message; }
    }
  }
}'''

_IFILEOP_PS = "Add-Type -Language CSharp -TypeDefinition @'\n" + _IFILEOP_CS + "\n'@\n"


def _nav_fragment(ps_parts):
    return (
        '$shell = New-Object -ComObject Shell.Application\n'
        "$folder = $shell.Namespace('::{20D04FE0-3AEA-1069-A2D8-08002B30309D}')\n"
        '$parts = @(' + ps_parts + ')\n'
        'foreach ($part in $parts) {\n'
        '    $found = $null\n'
        '    foreach ($item in $folder.Items()) {\n'
        '        if ($item.Name -eq $part) { $found = $item; break }\n'
        '    }\n'
        '    if (-not $found) {\n'
        '        foreach ($item in $folder.Items()) {\n'
        '            if ($item.Name -like ("*" + $part + "*")) { $found = $item; break }\n'
        '        }\n'
        '    }\n'
        f'    if (-not $found) {{ Write-Error ("Not found: " + $part); exit {_MTP_EXIT_NAV_FAIL} }}\n'
        '    $next = $found.GetFolder\n'
        f'    if (-not $next) {{ Write-Error ("Cannot open: " + $part); exit {_MTP_EXIT_NAV_FAIL} }}\n'
        '    $folder = $next\n'
        '}\n'
    )


def copy_kmz_to_mtp(rc_dir, uuid_name, tmp_kmz, tmp_dir):
    rc_norm = rc_dir.replace('/', '\\')
    if rc_norm.lower().startswith('this pc\\'):
        rc_norm = rc_norm[len('this pc\\'):]
    parts = [p for p in rc_norm.split('\\') if p]
    ps_parts = ', '.join("'" + p.replace("'", "''") + "'" for p in parts)
    tmp_kmz_ps = tmp_kmz.replace('/', '\\')
    copy_ps = os.path.join(tmp_dir, 'copy_kmz.ps1')
    with open(copy_ps, 'w', encoding='utf-8') as fh:
        fh.write(
            _IFILEOP_PS +
            _nav_fragment(ps_parts) +
            "$uuid = '" + uuid_name.replace("'", "''") + "'\n"
            '$uuidItem = $null\n'
            'foreach ($item in $folder.Items()) {\n'
            '    if ($item.Name -eq $uuid) { $uuidItem = $item; break }\n'
            '}\n'
            f'if (-not $uuidItem) {{ Write-Error "UUID folder not found on RC"; exit {_MTP_EXIT_UUID_MISSING} }}\n'
            "$res = [MissionPlannerIFO.Op]::CopyTo('" + tmp_kmz_ps.replace("'", "''") +
            "', $uuidItem, ($uuid + '.kmz'))\n"
            'if ($res -eq "OK") { Write-Output "OK" }\n'
            f'else {{ Write-Error $res; exit {_MTP_EXIT_UUID_NO_OPEN} }}\n'
        )
    try:
        r2 = subprocess.run(
            [_powershell(), '-NoProfile', '-NonInteractive', '-STA',
             '-ExecutionPolicy', 'Bypass', '-File', copy_ps],
            capture_output=True, text=True, timeout=60,
            creationflags=_NO_WINDOW, startupinfo=_STARTUPINFO)
    except subprocess.TimeoutExpired:
        return False, 'Copy to RC timed out. Check the RC is still connected.'
    except Exception as exc:
        return False, f'Copy error: {exc}'
    if r2.returncode == 0 and 'OK' in r2.stdout:
        return True, uuid_name
    return False, (f'Copy to RC failed (exit {r2.returncode}). '
                   f'{r2.stderr.strip() or r2.stdout.strip()}')


def replace_controller(waypoint_path, uuid, data):
    if not _UUID_RE.match(uuid or ''):
        return False, 'Choose a mission on the RC.'
    if os.path.isdir(waypoint_path):
        folder = os.path.join(waypoint_path, uuid)
        if not os.path.isdir(folder):
            return False, f'Mission folder not found: {folder}'
        try:
            with open(os.path.join(folder, uuid + '.kmz'), 'wb') as f:
                f.write(data)
        except Exception as exc:
            return False, str(exc)
        return True, uuid
    temp_dir = tempfile.mkdtemp(prefix='mission_planner_')
    try:
        kmz = os.path.join(temp_dir, uuid + '.kmz')
        with open(kmz, 'wb') as f:
            f.write(data)
        return copy_kmz_to_mtp(waypoint_path, uuid, kmz, temp_dir)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def detect():
    path = find_waypoint_on_drives()
    if path:
        status, missions = list_missions_from_dir(path)
        return {'status': status, 'path': path, 'missions': missions, 'detail': ''}
    status, path, missions, detail = list_rc_missions()
    return {'status': status, 'path': path, 'missions': missions, 'detail': detail}
