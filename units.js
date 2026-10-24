// How the pages show readings: [units] in config.ini (units.py says more).
// The station keeps every reading in one set of units (°F, mph, inHg, inches,
// miles, feet); the page's data carries the choice as `units`, and
// Units.use(data.units) switches to it. Each kind: for each choice, the factor
// from the stored unit, the offset, the label and the decimal places shown, the
// same as units.py KINDS.
"use strict";
const Units = (() => {
  const KINDS = {
    temperature: { f: [1, 0, "°F", 1], c: [5 / 9, -32 * 5 / 9, "°C", 1] },
    wind: { mph: [1, 0, " mph", 0], kmh: [1.609344, 0, " km/h", 0], ms: [0.44704, 0, " m/s", 1],
            knots: [0.868976, 0, " kn", 0] },
    pressure: { inhg: [1, 0, " inHg", 2], hpa: [33.8639, 0, " hPa", 1], mmhg: [25.4, 0, " mmHg", 1] },
    rain: { in: [1, 0, " in", 2], mm: [25.4, 0, " mm", 1] },
    distance: { mi: [1, 0, " mi", 0], km: [1.609344, 0, " km", 0] },
    // Heights follow the distance: feet with miles, metres with kilometres.
    height: { mi: [1, 0, " ft", 0], km: [0.3048, 0, " m", 0] },
  };
  let units = { temperature: "f", wind: "mph", pressure: "inhg", rain: "in", distance: "mi" };
  const unit = kind => KINDS[kind][units[kind === "height" ? "distance" : kind]] || Object.values(KINDS[kind])[0];
  const missing = v => v == null || v === "" || isNaN(v);
  return {
    use(chosen) { if (chosen) units = { ...units, ...chosen }; },
    key: () => JSON.stringify(units),
    // A stored value in the chosen unit; change: a difference, which takes no offset.
    value(kind, v, change = false) {
      if (missing(v)) return null;
      const [factor, offset] = unit(kind);
      return Number(v) * factor + (change ? 0 : offset);
    },
    places: kind => unit(kind)[3],
    label: kind => unit(kind)[2].trim(),
    // "31.5" (no label), for a place that writes its own, like a big temperature.
    num(kind, v, places) {
      const x = this.value(kind, v);
      return x == null ? "—" : x.toFixed(places ?? unit(kind)[3]);
    },
    // "31.5°F", "12 mph", "29.92 inHg"; "—" for nothing.
    text(kind, v, places) {
      const x = this.value(kind, v);
      return x == null ? "—" : x.toFixed(places ?? unit(kind)[3]) + unit(kind)[2];
    },
  };
})();
