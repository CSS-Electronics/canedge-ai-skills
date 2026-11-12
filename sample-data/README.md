# Sample data

A minimal CANedge dataset to try the skills and to self-test the setup (`check_setup.py --selftest`), from our free
[EV data pack](https://www.csselectronics.com/pages/ev-data-pack-electric-vehicles):

| Item | Content |
|---|---|
| `2F6913DB/00000595/*.MF4` | 3 log files (2.2 MB, 39 min): A Kia EV6 drive, CANedge2 + CANmod.gps. CAN1 = UDS requests/responses (battery, temperatures, speed), CAN2 = GNSS/IMU from the CANmod.gps |
| `dbc/can1-hyundai-kia-uds-v2.4.dbc` | Decodes the UDS responses on CAN1 (e.g. battery current/voltage, cell voltages, SoC, vehicle speed) |
| `dbc/can2-canmod-gnss.dbc` | Decodes the CANmod.gps messages on CAN2 (position, speed, altitude, attitude, IMU) |

The folder layout is the S3 layout (`<device>/<session>/<split>-<upload suffix>.MF4`). Both DBCs apply to the one
device. Example prompts:

> Give me an overview of the raw CAN data in sample-data

> Decode sample-data with the DBC files in sample-data/dbc and show me battery current vs speed during the drive
