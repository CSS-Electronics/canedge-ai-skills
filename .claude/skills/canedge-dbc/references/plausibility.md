# Does a DBC decode to sensible values? (data check)

Runs on a decoded lake with canedge-lake-query (local, Athena, BigQuery or Synapse), on one device and a short window that
covers the relevant operation (e.g. a drive):

1. `lake_schema.py --catalog --device X --dbc <p> --start .. --end ..`: Which DBC messages have tables, the units,
   and the signals that are always NULL
2. `lake_schema.py --stats <table> --start .. --end ..`: Per signal the NULL share, min/max/mean and distinct values
3. Compare each range with its unit and the table below; check NULL shares and constants (rules below)
4. Suspected scale/unit error: `lake_query.py resample` of the signal and a reference signal on the same window
   (e.g. `--interval 1s`), then the median ratio and correlation of the pairs in a few lines of pandas (skip
   pairs near zero)

## Reading the numbers

- **CSS decoders write NULL for values outside the DBC min/max** (rows where all signals are NULL are skipped). So a
  wrong range or scale often shows up as NULLs, not as odd values: A high NULL share, or values piling up at the DBC
  max, means the range may be too narrow. Never "fix" a range by narrowing it without data
- Always NULL: Not sent / not available on this vehicle (e.g. J1939 0xFF.. codes), or every value outside min/max
- Constant: Often legitimate (status bits, not-available signals); a constant "all ones" raw value hints at a
  not-available code
- A jumpy signal (consecutive samples jump across the whole range) hints at a wrong byte order or start bit, unless
  it is a counter, CRC or ID
- A flag is a lead, not proof: Confirm with a reference signal, the raw bytes (canedge-mdf2csv) or the user's
  documentation before changing a DBC

## Physical ranges (deliberately wide: outside = check it)

| Kind | Plausible range |
|---|---|
| Temperature (degC) | -50..200; hot zones (exhaust, EGR, turbo, DPF/SCR, aftertreatment, manifold, brake, cylinder) -50..1000 |
| Cell voltage / voltage / current / power | 0..5 V (mV: 0..5000) / -60..1500 V / -3000..3000 A / -1500..1500 kW |
| SoC, SoH / percentage | 0..101 % / 0..100 % (J1939 load 0-250 %, torque -125..125 % are valid) |
| Speed | -50..400 km/h, -15..112 m/s, -30..250 mph |
| Engine / turbo / other rotational speed | -100..10000 rpm / 0..350000 rpm / -25000..25000 rpm |
| Latitude / longitude | -90..90 / -180..180 |

## Scale factor causes (median ratio of signal / reference)

| Ratio | Likely cause |
|---|---|
| 10, 100, 1000 (or 1/x) | Scale error (e.g. V vs mV) |
| 3.6 | km/h vs m/s (CANedge GNSS speed is m/s) |
| 1.609 | km/h vs mph |
| -1 | Sign inverted |
| 2, 4 | Bit shift (start bit) |
| 256 | Byte shift (start bit off by 8, or byte order) |

A correlation below ~0.8 means the two signals probably do not measure the same thing. Good reference pairs:
Wheel-based vs axle or GNSS speed, engine speed vs transmission input speed, pack power vs voltage x current.
