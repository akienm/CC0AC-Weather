# Google Play listing — CC0AC Weather

Everything Play Console asks for on the store listing and the app content pages,
ready to paste. Graphics are beside this file.

## Store listing

**App name** (30 max): `CC0AC Weather`

**Short description** (80 max):
`Your own weather station on your phone: a home-screen widget or a wall display.`

**Full description** (4000 max):

```
CC0AC Weather shows the readings from YOUR weather station: the one in your yard,
read by the free CC0AC Weather station software running on a computer in your house.
No account, no cloud service in between, no ads, nothing collected.

TWO WAYS TO USE IT

• A home-screen widget. Small, it shows the temperature and how it feels. Make it
  wider for today's high and low and the wind; taller for the next hours, then the
  coming days. Place as many as you like, each showing its own sensor.

• Kiosk mode. Turn a spare phone into a wall display: your station's dashboard full
  screen, going dark when nobody is near and waking at a touch. Android pins it to the
  screen and asks you first; Back leaves it. For a display nobody can leave, set up a
  freshly reset phone with the setup script from a computer.

WHAT YOU NEED

This app reads a CC0AC Weather station. It does not work on its own. The station
software is free and open source (MIT) and runs on a Raspberry Pi or any Linux,
Mac or Windows computer. It takes your AcuRite Access hub's readings directly, so
they keep coming even without AcuRite's cloud:
https://github.com/akienm/CC0AC-Weather

Away from home, the app can reach your station only if you've given it a way in,
such as Tailscale or a port forward on your router. The station's README says how.

If the station can't be reached, the app says why in plain words: an untrusted
certificate, a certificate for another name, the wrong kind of address, or nothing
answering.
```

**App category:** Weather
**Tags:** Weather, Widgets
**Contact email:** *(Akien's choice; Play shows it publicly)*
**Website:** https://github.com/akienm/CC0AC-Weather
**Privacy policy URL:** https://github.com/akienm/CC0AC-Weather/blob/main/PRIVACY.md

**Graphics**
- App icon, 512×512: `icon-512.png` (master drawing: `icon.svg`)
- Feature graphic, 1024×500: `feature-graphic.png`
- Phone screenshots (Play requires 2–8, at most 2:1): `screenshots/`
  1. kiosk mode, portrait  2. kiosk mode, landscape  3. the app's own page
  Taken on the Android 16 emulator against a live station.

## App content

**Privacy policy:** the URL above.
**Ads:** No, the app contains no ads.
**App access:** All functionality is available without special access. *(It needs a
station address to show anything; the reviewer note below says so.)*
**Reviewer note:** "The app shows readings from the user's own CC0AC Weather station
(free software, github.com/akienm/CC0AC-Weather). Without one, the app's page and the
widget's settings screen are all there is to see."
**Content rating questionnaire:** category *Utility, productivity, communication, or
other*; answer No to every question (no violence, sexuality, language, controlled
substances, gambling, user interaction, sharing of location, or purchases).
**Target audience:** 18 and over *(avoids the extra rules for apps aimed at
children; the app is a utility for station owners)*.
**News app:** No.
**Data safety:**
- Does the app collect or share any of the required user data types? **No.**
- Is all data encrypted in transit? Answer only if asked. The station address may be
  plain http on a home network, so the true answer is **No**.
- Account creation: **none**. Data deletion: not applicable (nothing collected).
**Government app:** No. **Financial features:** none. **Health:** none.
**Device admin / kiosk:** the app declares a device-admin receiver that becomes
active only when setup-phone.sh makes it the device owner over adb. If Play asks
about it, its purpose is a locked wall display the phone's owner sets up themselves.
