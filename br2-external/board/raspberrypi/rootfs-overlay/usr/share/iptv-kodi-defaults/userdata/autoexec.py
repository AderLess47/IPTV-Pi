# IPTV-Pi: Kodi startup hook.
#
# Kodi 18 embeds Python 2.7, so this file must stay Python 2 compatible.
# It makes sure the IPTV Simple Client is enabled and keeps pointing at the
# playlist that the web interface manages.

import json
import xbmc

ADDON = "pvr.iptvsimple"
PLAYLIST = "/var/lib/iptv/playlist.m3u"

# Only the settings this appliance owns: the playlist source. Everything
# else (EPG, logos, ...) is left at the add-on defaults.
SETTINGS = {
    "m3uPathType": "0",  # 0 = local file
    "m3uPath": PLAYLIST,
    "m3uCache": "false",
}


def rpc(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        xbmc.executeJSONRPC(json.dumps(payload))
    except Exception:
        pass


xbmc.log("IPTV-Pi: configuring IPTV Simple Client", level=xbmc.LOGINFO)

# Builtin exists since Kodi 18 and is a no-op if the add-on is already on.
try:
    xbmc.executebuiltin('EnableAddon("%s")' % ADDON)
except Exception:
    pass

for key in SETTINGS:
    rpc("Addons.SetAddonSetting",
        {"addonid": ADDON, "setting": key, "value": SETTINGS[key]})

rpc("Addons.SetAddonEnabled", {"addonid": ADDON, "enabled": True})

# Start the PVR manager so channels show up without a manual trip to
# Settings -> PVR.
try:
    xbmc.executebuiltin("StartPVRManager")
except Exception:
    pass

xbmc.log("IPTV-Pi: IPTV Simple Client configured", level=xbmc.LOGINFO)
