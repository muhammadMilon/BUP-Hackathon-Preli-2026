import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.directives import rule_based_interpret
from app.schemas import Battery

BAT = Battery(capacity_kwh=500, initial_energy_kwh=200, minimum_energy_kwh=50,
              max_charge_kwh_per_hour=100, max_discharge_kwh_per_hour=100)

CASES = [
 ("Solar output will drop to about 20% from 1 PM to 3 PM.", "solar_reduction", {"hours":[13,14],"factor":0.2}),
 ("Do not charge the battery between 2 PM and 4 PM.", "no_charge_window", {"hours":[14,15]}),
 ("Keep at least 120 kWh in reserve from 6 PM until 9 PM.", "minimum_battery_reserve", {"hours":[18,19,20],"minimum_energy_kwh":120.0}),
 ("The cafeteria menu changes tomorrow.", "no_op", None),
 ("Panel washing from one until three will leave roughly one-fifth of normal solar output.", "solar_reduction", {"hours":[13,14],"factor":0.2}),
 ("Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window.", "solar_reduction", {"hours":[13,14],"factor":0.2}),
 ("PV production will drop to about 20% between 13:00 and 15:00.", "solar_reduction", {"hours":[13,14],"factor":0.2}),
 ("Grid import must not exceed 150 kWh from 9 AM to noon.", "max_grid_window", {"hours":[9,10,11],"max_grid_kwh":150.0}),
 ("The battery must not discharge between 7 AM and 10 AM.", "no_discharge_window", {"hours":[7,8,9]}),
 ("Cap grid draw at 220 kWh during the 6 PM to 9 PM evening peak.", "max_grid_window", {"hours":[18,19,20],"max_grid_kwh":220.0}),
 ("Charging is suspended from midnight to 5 AM for inverter firmware updates.", "no_charge_window", {"hours":[0,1,2,3,4]}),
 ("Cloud cover will halve solar generation from 10 AM to 1 PM.", "solar_reduction", {"hours":[10,11,12],"factor":0.5}),
 ("Never let the battery state of charge fall below 30% of capacity between 8 PM and 11 PM.", "minimum_battery_reserve", {"hours":[20,21,22],"minimum_energy_kwh":150.0}),
 ("An elevator inspection is scheduled in Building C tomorrow morning.", "no_op", None),
 ("Rooftop array will be completely offline from 12:00 to 14:00.", "solar_reduction", {"hours":[12,13],"factor":0.0}),
 ("Maintain a minimum of 200 kWh in the storage system from 5 PM to 8 PM.", "minimum_battery_reserve", {"hours":[17,18,19],"minimum_energy_kwh":200.0}),
 ("Please avoid topping up the battery from 11 AM to 1 PM.", "no_charge_window", {"hours":[11,12]}),
 ("Library WiFi will be upgraded next week.", "no_op", None),
 ("Transformer work limits utility import to no more than 90 kWh between 2 AM and 5 AM.", "max_grid_window", {"hours":[2,3,4],"max_grid_kwh":90.0}),
 ("Overcast skies should cut PV yield by 60% between 9 AM and noon.", "solar_reduction", {"hours":[9,10,11],"factor":0.4}),
 ("The battery cannot be used to serve load from 6 AM to 9 AM.", "no_discharge_window", {"hours":[6,7,8]}),
 ("Annual convocation rehearsal is in the auditorium at 4 PM.", "no_op", None),
]


def run():
    failures = 0
    for i, (note, dtype, adj) in enumerate(CASES):
        got = rule_based_interpret(note, i, BAT)
        ok = got["directive_type"] == dtype and got["structured_adjustment"] == adj
        if got["directive_type"] != "no_op":
            ok = ok and got["applies"] is True
        else:
            ok = ok and got["applies"] is False
        failures += not ok
        print(("OK  " if ok else "FAIL"), "{:24s}".format(got["directive_type"]),
              str(got["structured_adjustment"])[:56], "|", note[:52])
    print("\nrule-based interpreter: {}/{} passed".format(len(CASES) - failures, len(CASES)))
    return failures


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
