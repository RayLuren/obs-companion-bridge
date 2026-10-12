# Native menu plugin build

The supported baseline is Windows x64 and OBS Studio **32.2.1**. Install Visual
Studio Build Tools with **Desktop development with C++**, CMake 3.28 or newer,
and Ninja. The scripts discover the MSVC installation with `vswhere` rather
than assuming a particular Visual Studio release or installation folder.

From a normal command prompt at the repository root:

```bat
scripts\build_plugin.cmd --host-only
```

This builds and runs the native test host using `test_obs_api.h`. It requires
Windows and MSVC, but it does not download OBS or require an OBS installation.
The host simulates OBS APIs; passing it does not demonstrate real OBS loading
or live streaming.

For the actual plugin, first prepare the local reference directory. Use your
own installed OBS 32.2.1 `bin\64bit` directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\prepare_obs_sdk.ps1 -ObsBin 'D:\obs-studio\bin\64bit'
```

The preparation script downloads the official
[OBS 32.2.1 source archive](https://github.com/obsproject/obs-studio/releases/tag/32.2.1),
verifies its published SHA-256, extracts the required header trees, and generates import libraries from the exports
of your local `obs.dll` and `obs-frontend-api.dll`. It does not copy either DLL
or change the installed OBS. The generated `obsconfig.h` is only the minimal
configuration needed to compile this plugin; this project does not build OBS.

The default ignored local layout is:

```text
.deps/obs-32.2.1/
  obs-src/libobs/obs-module.h
  obs-src/frontend/api/obs-frontend-api.h
  generated/obsconfig.h
  generated/obs.lib
  generated/obs-frontend-api.lib
  reference.json
```

Then build the DLL and run the same host test:

```bat
scripts\build_plugin.cmd "%CD%\.deps\obs-32.2.1"
```

Output: `obs_plugin\build-full\obs-mate-menu.dll`. Build commands never install
or load the DLL automatically. `obs_plugin\build.cmd` is a convenience wrapper
for the same two forms.

To use a different reference location, pass `-ObsReference` to the preparation
script and the matching directory to the build script. Direct CMake users
must provide `-DOBS_REFERENCE=...` for a full build, or explicitly select
`-DBRIDGE_HOST_TEST_ONLY=ON`; there is no dependency on another checkout.

Do not commit `.deps`, generated headers, import libraries, DLLs, archives or
build directories. The imported OBS source remains subject to its upstream
license and is downloaded locally rather than vendored here.

Repository and prepared OBS headers have explicit build dependencies, including
on systems where localized MSVC include output cannot be parsed by Ninja. Use a
new build directory after upgrading the compiler or the Windows SDK.

## Initial release layout

This first publication retains the tested native launcher's fixed target:
`D:\OBS伴侣桥接\app\1.0.2\OBS伴侣桥接.exe`. Changing the checkout location does
not change this target. The plugin is not a portable installer; follow the
repository's application deployment instructions before installing it. This
build cleanup does not change the native menu, pipe protocol, release version,
or streaming behavior.
