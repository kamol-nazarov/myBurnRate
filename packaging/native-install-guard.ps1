param(
    [Parameter(Mandatory)]
    [ValidateSet('Install', 'Update', 'Uninstall', 'DeleteData')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'

# These native queries resolve DOS short names and let us check and stop a
# matching process using the same handle. The installer can be 32-bit even
# when the installed application is 64-bit.
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

public static class BurnrateInstallPaths
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(
        string path, uint access, uint share, IntPtr security, uint disposition,
        uint flags, IntPtr template);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandle(
        SafeFileHandle handle, StringBuilder path, uint size, uint flags);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern SafeFileHandle OpenProcess(uint access, bool inherit, int pid);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool QueryFullProcessImageName(
        SafeFileHandle handle, uint flags, StringBuilder path, ref uint size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(SafeFileHandle handle, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(SafeFileHandle handle, uint milliseconds);

    public static string CanonicalDirectory(string path)
    {
        using (var handle = CreateFile(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero))
        {
            if (handle.IsInvalid)
                throw new Win32Exception(Marshal.GetLastWin32Error());
            var result = new StringBuilder(32768);
            uint length = GetFinalPathNameByHandle(handle, result, (uint)result.Capacity, 0);
            if (length == 0 || length >= result.Capacity)
                throw new Win32Exception(Marshal.GetLastWin32Error());
            var dosPath = result.ToString();
            if (!dosPath.StartsWith(@"\\?\") || dosPath.StartsWith(@"\\?\UNC\"))
                throw new InvalidOperationException("The program directory must be on a local drive.");
            return dosPath.Substring(4);
        }
    }

    public static int MatchingProcesses(string executable, bool stop)
    {
        const uint Query = 0x1000;
        const uint Stop = 0x100001;
        int count = 0;
        foreach (var process in Process.GetProcessesByName("BURNRATE"))
        {
            using (process)
            using (var handle = OpenProcess(Query | (stop ? Stop : 0), false, process.Id))
            {
                uint size = 32768;
                var path = new StringBuilder((int)size);
                if (handle.IsInvalid)
                {
                    if (!stop)
                        continue;
                    using (var query = OpenProcess(Query, false, process.Id))
                    {
                        if (!query.IsInvalid && QueryFullProcessImageName(query, 0, path, ref size)
                            && String.Equals(path.ToString(), executable, StringComparison.OrdinalIgnoreCase))
                            throw new InvalidOperationException("Could not stop the installed application.");
                    }
                    continue;
                }
                if (!QueryFullProcessImageName(handle, 0, path, ref size)
                    || !String.Equals(path.ToString(), executable, StringComparison.OrdinalIgnoreCase))
                    continue;
                ++count;
                if (stop && WaitForSingleObject(handle, 0) != 0)
                {
                    if (!TerminateProcess(handle, 0) && WaitForSingleObject(handle, 0) != 0)
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    if (WaitForSingleObject(handle, 5000) != 0)
                        throw new InvalidOperationException("The installed application is still running.");
                }
            }
        }
        return count;
    }
}
'@

function Get-CanonicalFolder([string]$Path) {
    if ($Path -notmatch '^[A-Za-z]:[\\/]' -or $Path.Substring(2) -match '[:*?"<>|]') {
        throw 'Choose a directory on a local drive.'
    }
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($full)
    $segments = $full.Substring($root.Length).Split('\', [StringSplitOptions]::RemoveEmptyEntries)
    if (-not $segments.Count) {
        throw 'Choose a directory below the drive root.'
    }
    $current = $root
    foreach ($segment in $segments) {
        if ($segment.EndsWith('.') -or $segment.EndsWith(' ') -or
            $segment -match '^(CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])(\.|$)') {
            throw 'Choose a directory without Windows device names or trailing dots or spaces.'
        }
        $current = [IO.Path]::Combine($current, $segment)
        try {
            $attributes = [IO.File]::GetAttributes($current)
        } catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] {
            continue
        }
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -or
            -not ($attributes -band [IO.FileAttributes]::Directory)) {
            throw 'Choose a directory without symbolic links, junctions, or files in its path.'
        }
        $current = [BurnrateInstallPaths]::CanonicalDirectory($current)
    }
    return $current.TrimEnd('\')
}

function Test-PathWithin([string]$Parent, [string]$Path) {
    return $Path.Equals($Parent, [StringComparison]::OrdinalIgnoreCase) -or
        $Path.StartsWith($Parent + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparseChildren([string]$Path) {
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push($Path)
    while ($pending.Count) {
        foreach ($child in [IO.Directory]::EnumerateFileSystemEntries($pending.Pop())) {
            $attributes = [IO.File]::GetAttributes($child)
            if ($attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'The selected directory contains a symbolic link or junction.'
            }
            if ($attributes -band [IO.FileAttributes]::Directory) {
                $pending.Push($child)
            }
        }
    }
}

function Assert-RegisteredInstall([string]$Path) {
    $settings = Get-ItemProperty -LiteralPath 'HKCU:\Software\BURNRATE Native'
    $uninstall = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\BURNRATENative'
    if ($Path -ne (Get-CanonicalFolder $settings.InstallDir) -or
        $Path -ne (Get-CanonicalFolder $uninstall.InstallLocation)) {
        throw 'The selected directory is different from the registered myBurnRate installation.'
    }
    Assert-NoReparseChildren $Path
    foreach ($name in @('BURNRATE.installed', 'BURNRATE.exe', 'uninstall.exe')) {
        if (-not [IO.File]::Exists((Join-Path $Path $name))) {
            throw 'The selected directory is missing its myBurnRate installation files.'
        }
    }
    $marker = [IO.File]::ReadAllText((Join-Path $Path 'BURNRATE.installed')) | ConvertFrom-Json
    if ($marker.installer -ne 'nsis' -or -not $marker.version -or
        $marker.version -ne $settings.Version -or $marker.version -ne $uninstall.DisplayVersion) {
        throw 'The myBurnRate installation marker does not match its registration.'
    }
}

try {
    $program = Get-CanonicalFolder $env:BURNRATE_NSIS_PROGRAM_DIR
    $data = Get-CanonicalFolder $env:BURNRATE_NSIS_DATA_DIR
    if ((Test-PathWithin $data $program) -or (Test-PathWithin $program $data)) {
        throw 'Choose a program directory separate from the myBurnRate data directory.'
    }

    if ($Action -eq 'DeleteData') {
        if ([IO.Directory]::Exists($data)) {
            Assert-NoReparseChildren $data
        }
        exit 0
    }

    if ($Action -eq 'Install' -and (-not [IO.Directory]::Exists($program) -or
        -not ([IO.Directory]::EnumerateFileSystemEntries($program) | Select-Object -First 1))) {
        exit 0
    }

    Assert-RegisteredInstall $program
    $executable = Join-Path $program 'BURNRATE.exe'
    if ($Action -eq 'Update') {
        # Give the application window time to exit cleanly. The collector can
        # remain running until its own installed process is stopped below.
        $initialCount = [BurnrateInstallPaths]::MatchingProcesses($executable, $false)
        for ($i = 0; $i -lt 12 -and $initialCount -gt 0; $i++) {
            Start-Sleep -Milliseconds 250
            if ([BurnrateInstallPaths]::MatchingProcesses($executable, $false) -lt $initialCount) {
                break
            }
        }
    }
    [void][BurnrateInstallPaths]::MatchingProcesses($executable, $true)
    Assert-RegisteredInstall (Get-CanonicalFolder $env:BURNRATE_NSIS_PROGRAM_DIR)
    exit 2
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
