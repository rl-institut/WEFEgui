import os
import tempfile
from copy import deepcopy
from pathlib import Path

import datapackage as dp
import tableschema
import numpy as np
import pandas as pd
import logging
import json
import shutil

from projects.models import Timeseries, Scenario
from wefe.helpers import (
    AVAILABLE_COMPONENTS,
    AVAILABLE_SEQUENCES,
    COMPONENT_TEMPLATES_PATH,
    COMPONENT_HELPERS_PATH,
    create_components_list,
    WATER_TREATMENT_TRAIN,
    SURVEY_ANSWER_COMPONENT_MAPPING,
    SUB_QUESTION_MAPPING,
    get_renewables_output,
)

# TODO: additional imports/static files - > static/wefe_configurator/


# TODO this needs to work standalone as well as a service

# -------------RELEVANT PATHS------------
# they may be changed if this script is moved somewhere else...
script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(os.path.dirname(script_dir))
scenario_dir = os.path.join(project_dir, "scenarios")
lib_dir = os.path.join(project_dir, "component_library")

# -------SURVEY MAPPING----------
TYPE_FLOAT = "float"
TYPE_INT = "int"
TYPE_STRING = "string"
TYPE_WATER = "string"
INFOBOX = "description"

TYPE_COMPONENT = "component"
TYPE_COMPONENT_ATTRIBUTE = "attribute"
TYPE_NO_MAP = "skip"
TYPE_OTHER = "other"

type_check = {
    TYPE_FLOAT: float,
    TYPE_INT: int,
    TYPE_STRING: str,
}


class WEFEConfigurator:
    def __init__(
        self,
        scen_id,
        overwrite=False,
    ):
        self.scen_id = scen_id
        self.scenario = Scenario.objects.get(id=self.scen_id)
        self.proj_id = self.scenario.id
        self.overwrite = overwrite
        self.mapping = SURVEY_ANSWER_COMPONENT_MAPPING
        self.subq_mapping = SUB_QUESTION_MAPPING
        self.criterias = {}
        self.components = {}
        self.wished_components = {}
        self.additional_busses = []
        self.scenario_folder = self.create_scenario_folder()

    def create_scenario_folder(self):
        self._temp_dir = tempfile.TemporaryDirectory(prefix=f"wefeconf_{self.scen_id}_")
        scenario_folder = self._temp_dir.name

        os.makedirs(os.path.join(scenario_folder, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(scenario_folder, "data", "elements"), exist_ok=True)
        os.makedirs(os.path.join(scenario_folder, "data", "sequences"), exist_ok=True)

        return scenario_folder

    def cleanup(self):
        if self._temp_dir:
            self._temp_dir.cleanup()

    def water_systems_postprocessing(self, survey):

        def safety_check():
            # --- SAFETY CLEANUP STEP ---
            # Remove any existing water-treatment components that process_survey might have added
            water_main_list = []
            for comp in WATER_TREATMENT_TRAIN["main_list"]:
                if isinstance(comp, list):
                    water_main_list.extend(comp)
                else:
                    water_main_list.append(comp)
            for comp in water_main_list:
                self.components.pop((comp, comp), None)
            # --- END CLEANUP ---

        def fill_component_list(suffix):
            unique_slim_component_list = []
            combined_component_list = []
            if survey[f"4{suffix}.1"] is not None:
                salinity_value = survey[f"4{suffix}.1"]
                # print(salinity_value)  # float
                combined_component_list.extend(self.mapping[f"4{suffix}.1"]["map_answer"]["salinity_selected"])
            if survey[f"4{suffix}.2"] is not None:
                metals_selected = survey[f"4{suffix}.2"]
                for metal in metals_selected:
                    # print(metal)
                    combined_component_list.extend(self.mapping[f"4{suffix}.2"]["map_answer"][metal])
            if survey[f"4{suffix}.3"] is not None:
                chemicals_selected = survey[f"4{suffix}.3"]
                for chemical in chemicals_selected:
                    # print(chemical)
                    combined_component_list.extend(self.mapping[f"4{suffix}.3"]["map_answer"][chemical])
            if survey[f"5{suffix}"] and survey[f"5{suffix}"] not in (["no"], "no"):
                odd_tech = [tech.replace(" ", "_").replace("-", "_") for tech in survey[f"5{suffix}"]]
                combined_component_list.append(odd_tech)

            for item in combined_component_list:
                if isinstance(item, list):
                    unique_slim_component_list.extend(item)
                else:
                    unique_slim_component_list.append(item)

            unique_slim_component_list = list(dict.fromkeys(unique_slim_component_list))
            return unique_slim_component_list

        def arrange_components(main_list, component_list):
            arranged_component_list = []
            for component in main_list:
                if isinstance(component, list):  # parallel component adding
                    parallel_components = [parallel for parallel in component if parallel in component_list]
                    # single component from parallel choices
                    if len(parallel_components) == 1:
                        arranged_component_list.append(parallel_components[0])
                    # multiple components from parallel choices
                    elif len(parallel_components) > 1:
                        arranged_component_list.append(parallel_components)
                else:  # single component adding
                    if component in component_list:
                        arranged_component_list.append(component)

            return arranged_component_list

        def create_component_dict(component_list, entry_bus, water_type):
            prefix = "SW_" if water_type.lower() == "service" else "DW_"
            components_dict = {}
            counter = {}  # counts the number of times a component appears in the train
            previous_out_bus = entry_bus  # entry bus to the treatment train
            for index, component in enumerate(component_list):
                if isinstance(component, list):  # handling parallel components
                    parallel_out_bus = f"{prefix}parallel_{index + 1}_out_bus"
                    for parallel in component:
                        counter[parallel] = counter.get(parallel, 0) + 1
                        key = (f"{parallel}", f"{prefix}{parallel}_{counter[parallel]}")
                        components_dict[key] = {"water_in_bus": previous_out_bus, "water_out_bus": parallel_out_bus}
                    previous_out_bus = parallel_out_bus
                else:
                    counter[component] = counter.get(component, 0) + 1
                    out_bus = f"{prefix}{component}_{counter[component]}_out_bus"
                    key = (f"{component}", f"{prefix}{component}_{counter[component]}")
                    components_dict[key] = {"water_in_bus": previous_out_bus, "water_out_bus": out_bus}
                    previous_out_bus = out_bus

            exit_bus = "service-water-bus" if water_type.lower() == "service" else "drinking-water-bus"
            components_dict[next(reversed(components_dict.keys()))].update({"water_out_bus": exit_bus})

            return components_dict

        def update_component_parameters(suffixes, WT):
            capacity_sums = {}
            efficiency_values = {}
            specific_energy_consumption_values = {}
            mapping_dict = {
                "reverse osmosis": "reverse_osmosis",
                "membrane distillation": "membrane_distillation",
                "ultrafiltration": "ultrafiltration",
                "boiling": "boiling",
                "distillation": "distillation",
                "activated carbon filter": "activated_carbon_filter",
                "uv-disinfection": "uv_disinfection",
                "cartridge filter": "cartridge_filter",
                "microfiltration": "microfiltration",
                "ceramic filter": "ceramic_filter",
                "nanofiltration": "nanofiltration",
                "electrodialysis": "electrodialysis",
                "slow sand filter": "slow_sand_filter",
                "water softener": "ion_exchange",
                "chlorination": "chlorination",
            }
            for sfx in suffixes:
                if not survey[f"5{sfx}"] or survey[f"5{sfx}"] == ["no"]:
                    continue
                idx = 0
                for answer, facade in mapping_dict.items():
                    if answer in survey[f"5{sfx}"]:
                        comp_key = (facade, f"{WT}_{facade}_1")
                        if survey[f"5{sfx}.2.{idx}"] not in (None, "", " "):
                            capacity_sums[comp_key] = capacity_sums.get(comp_key, 0.0) + survey[f"5{sfx}.2.{idx}"]
                        if survey[f"5{sfx}.3.{idx}"] not in (None, "", " "):
                            # Keep highest specific energy consumption
                            if specific_energy_consumption_values.get(comp_key) is None or survey[
                                f"5{sfx}.3.{idx}"
                            ] > specific_energy_consumption_values.get(comp_key):
                                specific_energy_consumption_values[comp_key] = survey[f"5{sfx}.3.{idx}"]
                        try:
                            if survey[f"5{sfx}.1.{idx}"] not in (None, "", " "):
                                # Keep lowest efficiency value
                                if efficiency_values.get(comp_key) is None or survey[
                                    f"5{sfx}.1.{idx}"
                                ] < efficiency_values.get(comp_key):
                                    efficiency_values[comp_key] = survey[f"5{sfx}.1.{idx}"]
                        except KeyError:
                            pass
                    idx += 1

            # After all suffixes processed, update component attributes once with aggregated values
            for comp_key in capacity_sums:
                self.components[comp_key].update({"capacity": capacity_sums[comp_key]})
                if comp_key in specific_energy_consumption_values:
                    self.components[comp_key].update(
                        {"specific_energy_consumption": specific_energy_consumption_values[comp_key]}
                    )
                if comp_key in efficiency_values:
                    self.components[comp_key].update({"efficiency": efficiency_values[comp_key]})

        def add_excess():
            for component_key in list(self.components.keys()):
                component_type, component_name = component_key
                if component_type == "biological_denitrification":
                    self.add_single_component(component_type="excess-N2")
                if component_type == "biofiltration":
                    self.add_single_component(component_type="excess-biomass")
                if component_type in {
                    "microfiltration",
                    "ultrafiltration",
                    "nanofiltration",
                    "electrodialysis",
                    "distillation",
                    "membrane_distillation",
                    "reverse_osmosis",
                }:
                    self.add_single_component(component_type="brine-excess")

        def handle_water_criterion(crit, crit_label, suffixes, type_water):
            water_sources = [
                "groundwater well",
                "desalinated seawater",
                "river/creek",
                "lake",
                "public tap water",
                "water truck",
                "rainwater harvesting",
                "bottled water",
            ]

            natural_group = water_sources[:4]
            artificial_group = water_sources[4:]

            has_natural_group = any(src in crit for src in natural_group)
            has_artificial_group = any(src in crit for src in artificial_group)
            if has_natural_group:
                water_component_list = []
                for suffix in suffixes:
                    water_component_list.extend(fill_component_list(suffix))
                water_component_list = list(dict.fromkeys(water_component_list))
                water_component_list = arrange_components(WATER_TREATMENT_TRAIN["main_list"], water_component_list)
                water_treatment_dict = create_component_dict(
                    water_component_list, entry_bus="untreated-water-bus", water_type=type_water
                )
                for (component_type, component_name), component_attrs in water_treatment_dict.items():
                    # Add the component from the train
                    self.add_single_component(component_type, component_name, component_attrs)
                    # Add the water in and water out buses for each component
                    for bus_type in ["water_in_bus", "water_out_bus"]:
                        if component_attrs.get(bus_type):  # only add if defined
                            self.add_single_bus(name=component_attrs.get(bus_type), balanced=True, carrier="water")
                WT = "SW" if type_water == "service" else "DW"
                update_component_parameters(suffixes, WT)
                # add excess for drinking/service water
                self.add_single_component(component_type=f"excess-{type_water}-water")
            if has_artificial_group:
                pass  # survey answer component mapping takes care of it automatically
            if not (has_natural_group or has_artificial_group):
                raise ValueError(f"Criteria {crit_label} has no valid programmed water source selected")

        # ---Main Logic & Function Calling---

        # Strip 'criteria_' from keys locally
        survey = {k.replace("criteria_", ""): v for k, v in survey.items()}

        safety_check()

        if survey["2"] == "Yes":  # set Yes currently
            crit3 = survey["3a"]  # drinking water
            handle_water_criterion(
                crit3, crit_label="3a", suffixes=["_GWa", "_DSa", "_RCa", "_La"], type_water="drinking"
            )
            crit3 = survey["3b"]  # service water
            handle_water_criterion(
                crit3, crit_label="3b", suffixes=["_GWb", "_DSb", "_RCb", "_Lb"], type_water="service"
            )
        else:
            crit3 = survey["3"]  # all water assumed drinking water
            handle_water_criterion(crit3, crit_label="3", suffixes=["_GW", "_DS", "_RC", "_L"], type_water="drinking")

        add_excess()
        # ---------------------------------

    def water_systems_simplification(self):

        elements_dir = self.scenario_component_folder
        dp_json_path = os.path.join(self.scenario_folder, "datapackage.json")
        bus_path = os.path.join(elements_dir, "bus.csv")

        original_water_treatment_csvs = [
            "water_treatment_without.csv",
            "water_treatment_with_brine.csv",
            "water_treatment_with_biomass.csv",
            "water_treatment_with_N2.csv",
        ]

        extra_bus_columns = ["brine_out_bus", "waste_biomass_out_bus", "N2_gas_bus"]

        def modify_bus_csv(has_sw):
            df_bus = pd.read_csv(bus_path, sep=";")
            df_bus = df_bus[~df_bus["name"].str.startswith(("DW_", "SW_"))]

            bus_names = ["DW_pre_treatment_out_bus", "DW_core_treatment_out_bus"]

            if has_sw:
                bus_names.extend(["SW_pre_treatment_out_bus", "SW_core_treatment_out_bus"])

            simplified_buses = pd.DataFrame({"name": bus_names, "type": "bus", "balanced": True, "carrier": "water"})

            df_bus = pd.concat([df_bus, simplified_buses], ignore_index=True)
            df_bus = df_bus.drop_duplicates(subset=["name"], keep="last")
            df_bus.to_csv(bus_path, sep=";", index=False)

        def modify_water_treatment(water_treatment_csvs, extra_cols):
            has_sw_detected = False
            if os.path.exists(os.path.join(elements_dir, "water_treatment_without.csv")):
                df_water_without = pd.read_csv(os.path.join(elements_dir, "water_treatment_without.csv"), sep=";")
                if "name" in df_water_without.columns:
                    has_sw_detected = df_water_without["name"].str.startswith("SW_").any()

            dfs = []
            for csv_name in water_treatment_csvs:
                path = os.path.join(elements_dir, csv_name)
                if os.path.exists(path):
                    df = pd.read_csv(path, sep=";")
                    dfs.append(df)
                    try:
                        os.remove(path)
                        print(f"Deleted {csv_name}")
                    except OSError as e:
                        print(f"Could not delete {csv_name}: {e}")

            if not dfs:
                print("No water treatment CSVs found.")
                return None, False

            merged_df = pd.concat(dfs, ignore_index=True, sort=False)

            common_cols = [col for col in merged_df.columns if col not in extra_cols]
            ordered_cols = common_cols + [col for col in extra_cols if col in merged_df.columns]

            merged_df = merged_df[ordered_cols]

            pre_treatment_df = merged_df[
                (merged_df["type"].isin(WATER_TREATMENT_TRAIN["pre_treatment"]))
                & (merged_df["name"].str.endswith("_1"))
            ]
            core_treatment_df = merged_df[
                (merged_df["type"].isin(WATER_TREATMENT_TRAIN["core_treatment"]))
                & (merged_df["name"].str.endswith("_1"))
            ]
            post_treatment_df = merged_df[
                (merged_df["type"].isin(WATER_TREATMENT_TRAIN["post_treatment"]))
                | (merged_df["name"].str.endswith("_2"))
            ]
            pre_treatment_df.to_csv(os.path.join(elements_dir, "water_pre_treatment.csv"), sep=";", index=False)
            core_treatment_df.to_csv(os.path.join(elements_dir, "water_core_treatment.csv"), sep=";", index=False)
            post_treatment_df.to_csv(os.path.join(elements_dir, "water_post_treatment.csv"), sep=";", index=False)

            return merged_df, has_sw_detected

        # TODO: Improve the aggregation/compression logic in the following function for each of the three water treatment sections.

        def aggregate_component_block(df, prefix, block_name, water_in_bus, water_out_bus):
            sub = df[df["name"].str.startswith(prefix)].copy()

            if sub.empty:
                return None

            row = {}

            row["name"] = f"{prefix}{block_name}"
            row["type"] = f"water_{block_name}"
            row["water_in_bus"] = water_in_bus
            row["water_out_bus"] = water_out_bus

            numeric_sum_cols = [
                "capex",
                "opex_fix",
                "annuity",
                "specific_energy_consumption",
                "land_requirement_factor",
                "ghg_emission_factor",
                "water_consumption_factor",
            ]

            for col in numeric_sum_cols:
                if col in sub.columns:
                    row[col] = sub[col].fillna(0).sum()

            if "efficiency" in sub.columns:
                eff = sub["efficiency"].dropna()
                row["efficiency"] = eff.prod() if not eff.empty else None

            if "lifetime" in sub.columns:
                life = sub["lifetime"].dropna()
                if not life.empty:
                    if "capex" in sub.columns and sub["capex"].notna().any():
                        weights = sub.loc[life.index, "capex"].fillna(0)
                        if weights.sum() > 0:
                            row["lifetime"] = (life * weights).sum() / weights.sum()
                        else:
                            row["lifetime"] = life.mean()
                    else:
                        row["lifetime"] = life.mean()

            for col in sub.columns:
                if col not in row:
                    non_null = sub[col].dropna()
                    row[col] = non_null.iloc[0] if not non_null.empty else None

            row_df = pd.DataFrame([row])
            row_df = row_df[[c for c in df.columns if c in row_df.columns]]  # reorder columns
            row_df = row_df.dropna(axis=1, how="all")  # drop empty columns
            return row_df

        def modify_data_package(water_treatment_csvs, extra_bus_columns):

            new_csv = [
                ("water_pre_treatment", "water_pre_treatment.csv"),
                ("water_core_treatment", "water_core_treatment.csv"),
                ("water_post_treatment", "water_post_treatment.csv"),
            ]

            old_water_treatment_csvs = [f.removesuffix(".csv") for f in water_treatment_csvs]

            with open(dp_json_path, "r", encoding="utf-8") as f:
                datapackage = json.load(f)

            resources = datapackage["resources"]
            template = next(r for r in resources if r["name"] == "water_treatment_without")
            resources = [r for r in resources if r["name"] not in old_water_treatment_csvs]

            for new_name, new_path in new_csv:
                new_resource = deepcopy(template)
                new_resource["name"] = new_name
                new_resource["path"] = f"data/elements/{new_path}"

                csv_cols = pd.read_csv(os.path.join(elements_dir, new_path), sep=";", nrows=0).columns.tolist()

                for col in extra_bus_columns:
                    if col in csv_cols:
                        new_resource["schema"]["fields"].append({"name": col, "type": "string", "format": "default"})
                        new_resource["schema"]["foreignKeys"].append(
                            {"fields": col, "reference": {"resource": "bus", "fields": "name"}}
                        )

                resources.append(new_resource)

            datapackage["resources"] = resources

            with open(dp_json_path, "w", encoding="utf-8") as f:
                json.dump(datapackage, f, indent=4, ensure_ascii=False)

        # ---Main Logic & Function Calling---

        merged_df, has_sw = modify_water_treatment(original_water_treatment_csvs, extra_bus_columns)

        # Exit simplification if no water treatment csvs are detected
        # Modification of bus csv and datapackage json is avoided
        if merged_df is None:
            return

        modify_bus_csv(has_sw)

        water_csv_configs = [
            {
                "df": pd.read_csv(os.path.join(elements_dir, "water_pre_treatment.csv"), sep=";"),
                "block_name": "pre_treatment",
                "csv_name": "water_pre_treatment.csv",
                "buses": {
                    "DW_": ("untreated-water-bus", "DW_pre_treatment_out_bus"),
                    "SW_": ("untreated-water-bus", "SW_pre_treatment_out_bus"),
                },
            },
            {
                "df": pd.read_csv(os.path.join(elements_dir, "water_core_treatment.csv"), sep=";"),
                "block_name": "core_treatment",
                "csv_name": "water_core_treatment.csv",
                "buses": {
                    "DW_": ("DW_pre_treatment_out_bus", "DW_core_treatment_out_bus"),
                    "SW_": ("SW_pre_treatment_out_bus", "SW_core_treatment_out_bus"),
                },
            },
            {
                "df": pd.read_csv(os.path.join(elements_dir, "water_post_treatment.csv"), sep=";"),
                "block_name": "post_treatment",
                "csv_name": "water_post_treatment.csv",
                "buses": {
                    "DW_": ("DW_core_treatment_out_bus", "drinking-water-bus"),
                    "SW_": ("SW_core_treatment_out_bus", "service-water-bus"),
                },
            },
        ]

        for config in water_csv_configs:
            reduced_parts = []

            for prefix, (water_in_bus, water_out_bus) in config["buses"].items():
                reduced_df = aggregate_component_block(
                    config["df"],
                    prefix=prefix,
                    block_name=config["block_name"],
                    water_in_bus=water_in_bus,
                    water_out_bus=water_out_bus,
                )
                if reduced_df is not None:
                    reduced_parts.append(reduced_df)

            if reduced_parts:
                reduced_block_df = pd.concat(reduced_parts, ignore_index=True)
                reduced_block_df.to_csv(os.path.join(elements_dir, config["csv_name"]), sep=";", index=False)

        modify_data_package(original_water_treatment_csvs, extra_bus_columns)
        # ---------------------------------

    def waste_water_systems_postprocessing(self, survey):

        # Strip 'criteria_' from keys locally
        survey = {k.replace("criteria_", ""): v for k, v in survey.items()}

        def safety_check():
            # --- SAFETY CLEANUP STEP ---
            # Remove any existing wastewater-treatment components that process_survey might have added
            for comp in [
                "septic_system",
                "constructed_wetland",
                "centralized_WWTP",
                "decentralized_WWTP",
                "water_reuse_system",
            ]:
                self.components.pop((comp, comp), None)
            # --- END CLEANUP ---

        safety_check()

        def default_toilet_handling(toilet_types, population, cattle):
            if "dry toilet" not in toilet_types:
                self.add_single_component(component_type="dry_toilet")
                self.add_single_component(component_type="hu_waste", component_attrs={"capacity": population})
                self.add_single_component(component_type="hf_waste", component_attrs={"capacity": population})
                self.add_single_component(component_type="excess-dry-feces")
                self.add_single_component(component_type="excess-human-feces")
                self.add_single_component(component_type="excess-human-urine")

            if "open field" not in toilet_types:
                self.add_single_component(component_type="open_field")
                self.add_single_component(component_type="hu_waste", component_attrs={"capacity": population})
                self.add_single_component(component_type="hf_waste", component_attrs={"capacity": population})
                self.add_single_component(component_type="au_waste", component_attrs={"capacity": cattle})
                self.add_single_component(component_type="af_waste", component_attrs={"capacity": cattle})
                self.add_single_component(component_type="excess-biomass")
                self.add_single_component(component_type="excess-human-feces")
                self.add_single_component(component_type="excess-human-urine")
                self.add_single_component(component_type="excess-animal-feces")
                self.add_single_component(component_type="excess-animal-urine")

        wastewater_systems = survey["7"]
        population = self.scenario.project.economic_data.population  # population is WEFEgui input
        cattle = (
            population / 10
        )  # TODO: ask about cattle or model animal farming, current assumption: 1 cow for 10 people
        toilet_types = survey["7.3"]
        # print(toilet_types)

        default_toilet_handling(toilet_types, population, cattle)

        # black water treatment
        if "flush toilet" in toilet_types:
            if "constructed wetland" in wastewater_systems:
                component_key = self.add_single_component(
                    component_type="constructed_wetland",
                    component_name="black_water_cw",
                    component_attrs={"water_in_bus": "black-water-bus", "water_out_bus": "wwtp-ip-water-bus"},
                )
                capacity = survey["7.1.1"]
                if capacity not in (None, "", " "):
                    self.components[component_key].update({"capacity": capacity})
            else:
                # default addition of septic system
                component_key = self.add_single_component(
                    component_type="septic_system",
                    component_name="black_water_septic",
                    component_attrs={"water_in_bus": "black-water-bus", "water_out_bus": "wwtp-ip-water-bus"},
                )
                capacity = survey["7.1.0"]
                if capacity not in (None, "", " "):
                    self.components[component_key].update({"capacity": capacity})

        # grey water treatment

        if "constructed wetland" in wastewater_systems:
            component_key = self.add_single_component(
                component_type="constructed_wetland",
                component_name="grey_water_cw",
                component_attrs={"water_in_bus": "grey-water-bus", "water_out_bus": "wwtp-ip-water-bus"},
            )
            capacity = survey["7.1.1"]
            if capacity not in (None, "", " "):
                self.components[component_key].update({"capacity": capacity})

        else:
            # default addition of septic system
            component_key = self.add_single_component(
                component_type="septic_system",
                component_name="grey_water_septic",
                component_attrs={"water_in_bus": "grey-water-bus", "water_out_bus": "wwtp-ip-water-bus"},
            )
            self.add_single_component(component_type="hh_gw_waste")
            capacity = survey["7.1.0"]
            if capacity not in (None, "", " "):
                self.components[component_key].update({"capacity": capacity})

        # waste water treatment plant based on population  #assumed that at least either of the following is present, improve in future
        if population >= 10000:
            # centralized waste water treatment plant
            component_key = self.add_single_component(
                component_type="centralized_WWTP",
                component_attrs={"water_in_bus": "wwtp-ip-water-bus", "water_out_bus": "wwtp-op-water-bus"},
            )
            capacity = survey["7.1.2"]
            if capacity not in (None, "", " "):
                self.components[component_key].update({"capacity": capacity})
        else:
            # decentralized waste water treatment plant
            # default addition of this component
            component_key = self.add_single_component(
                component_type="decentralized_WWTP",
                component_attrs={"water_in_bus": "wwtp-ip-water-bus", "water_out_bus": "wwtp-op-water-bus"},
            )
            capacity = survey["7.1.3"]
            if capacity not in (None, "", " "):
                self.components[component_key].update({"capacity": capacity})

        # water recycling and reuse system #assumed that the reuse of water is always there
        # default addition of this component
        component_key = self.add_single_component(
            component_type="water_reuse_system",
            component_attrs={"water_in_bus": "wwtp-op-water-bus", "water_out_bus": "service-water-bus"},
        )
        capacity = survey["7.1.4"]
        if capacity not in (None, "", " "):
            self.components[component_key].update({"capacity": capacity})
        # add excess for service water
        self.add_single_component(component_type="excess-service-water")

        if (
            "disposal to environment without treatment" in wastewater_systems
        ):  # set direct disposal for grey water and black water
            self.add_single_component(component_type="greywater_disposal")
            if "flush toilet" in toilet_types:
                self.add_single_component(component_type="blackwater_disposal")

    def get_single_component_from_datapackage(self, dp, resource_name, component_name):
        """
        Returns a component from a single resource (csv file) of a datapackage as a one-row df.
        ATTENTION: the returned df will be empty if there is no match!
        """
        resource = dp.get_resource(resource_name)
        df = pd.DataFrame.from_records(resource.read(keyed=True))
        component = df[df["name"] == component_name]
        # TODO: check uniqueness of resource names
        #  (if necessary...check if there are already other checks for uniqueness of names in the reference datapackage)

        return component

    @property
    def reference_datapackage(self):
        # TODO maybe it would be good to save the datapackage json in the db for the scenario (maybe in a WEFESimulation object?)
        dp_json = os.path.join(COMPONENT_TEMPLATES_PATH, "datapackage.json")
        return dp.Package(dp_json)

    @property
    def scenario_datapackage(self):
        dp_json = os.path.join(self.scenario_folder, "datapackage.json")
        if os.path.exists(dp_json):
            answer = dp.Package(dp_json)
        else:
            answer = dp.Package(base_path=self.scenario_folder)
        return answer

    @property
    def scenario_component_folder(self):
        return os.path.join(self.scenario_folder, "data", "elements")

    @property
    def demand_data(self):
        # This data is now taken from the database instead of CSV
        timeseries_suffix = "_ramp_demand_agg_mean"
        qs_ts = Timeseries.objects.filter(scenario__id=self.scen_id, name__contains=timeseries_suffix)
        timeseries = {ts.name.replace(timeseries_suffix, ""): ts.values for ts in qs_ts}

        return pd.DataFrame(timeseries)

    @property
    def weather_data(self):
        # This data is now taken from the database instead of CSV
        timeseries = get_renewables_output(self.proj_id)
        timeseries_df = pd.DataFrame(timeseries)
        timeseries_prefix = "weather_data_"
        param_cols = [col.replace(timeseries_prefix, "") for col in timeseries_df.columns]
        timeseries_df.columns = param_cols
        return timeseries_df

    @property
    def waste_data(self):
        # Waste data stored in static files
        waste_data_path = Path(COMPONENT_HELPERS_PATH) / "waste_data.parquet"
        return pd.read_parquet(waste_data_path)

    @property
    def process_weather_data(self):
        """
        Function to calculate and add new columns to the weather_data DataFrame
        """

        df = self.weather_data.copy()
        c_j_to_wh = 1 / 3600
        offset_K_Celsius = 273.15

        if "ghi" not in df.columns:
            df["ghi"] = df["ssrd"] * c_j_to_wh

        if "t_air" not in df.columns:
            df["t_air"] = df["t2m"] - offset_K_Celsius

        if "t_dew" not in df.columns:
            df["t_dew"] = df["d2m"] - offset_K_Celsius

        if "windspeed" not in df.columns:
            df["windspeed"] = df.apply(lambda row: np.sqrt(row["u100"] ** 2 + row["v100"] ** 2), axis=1)

        return df

    def process_demand(self):
        """
        Add loads to the energy system based on demand_data
        Add excess and storages according to the loads based on the busses
        --------------------------
        ATTENTION regarding the variable names:
        "demand" refers to the actual data (from WEFEDemand)
        "load" refers to the load.csv from the component library
        profiles.csv works as a mapping: load_name -> demand_name

        TODO: more flexibility - this code depends on the csv names ["load", "excess", "storage", "profiles"],
         rather look for components of type "load", "excess", "storage" in all csv files
        """
        dp_ref = self.reference_datapackage

        # Read in demand from csv
        demand_df = self.demand_data

        # Merge drinking and service water demands if there is no differentiation
        if self.criterias["2"] == "No":
            demand_df["drinking_water"] += demand_df["service_water"]
            demand_df.drop("service_water", axis=1, inplace=True)

        # Read in relevant components from the component library
        resource_names = ["load", "excess", "storage", "profiles"]
        resources = {}
        for name in resource_names:
            resource = dp_ref.get_resource(name)
            df = pd.DataFrame.from_records(resource.read(keyed=True))
            resources[name] = df

        # Map the given demands to the corresponding loads of the component library
        load_profiles_to_add = []
        for demand in demand_df.columns:
            match = resources["profiles"].columns[(resources["profiles"].iloc[0] == demand).values].tolist()
            if not match:
                logging.warning(f"Demand profile '{demand}' not found in 'profiles.csv'")
            load_profiles_to_add.extend(match)

        # Get all relevant busses from the load profiles
        busses = set(resources["load"].loc[resources["load"]["profile"].isin(load_profiles_to_add), "bus"])

        # Collect all matching names across all resources
        unique_names = set()
        for df in resources.values():
            if "bus" not in df.columns or "name" not in df.columns:
                continue
            unique_names.update(df.loc[df["bus"].isin(busses), "name"])

        # Add each component only once
        for name in unique_names:
            self.add_single_component(name)

    def ensure_component(self, component):
        if not component or not str(component).strip():
            logging.warning("Attempted to add empty component")
            return None
        return self.components.setdefault((component, component), {})

    def process_survey(self, survey):
        """
        Process the survey responses to build a nested structure. Some answers add components, while some change
        the attributes of the components. The output structure allows to change all attributes in the .csv files at
        once without editing them multiple times.
        Example output:
        {
            "wind-turbine": {"capacity": 10, "some_parameter": 5},
            "diesel-generator": {"fuel_efficiency": 0.8}
        }
        If numeric attributes are assigned multiple times, the values will not be added up but the maximum value of all
        the assignments will be chosen instead.

        Additionally, certain answers defined in the 'criterias_list' are stored as class attributes 'self.criterias'
        to make them easily accessible.
        """
        criterias_list = ["2"]
        for question_id, answer in survey.items():
            # print(question_id)
            # 2 options for answer:
            # option 1: list -> turn all TYPE_COMPONENT answers into list
            # option 2: single item (None, float, str) -> assume all TYPE_COMPONENT_ATTRIBUTE answers to be single items
            if answer is not None:
                # obtain question_id
                question_id = question_id.strip("criteria_")

                if question_id in criterias_list:
                    self.criterias[question_id] = answer

                if question_id in self.mapping:

                    answer_mapping = self.mapping[question_id]
                    bus = answer_mapping.pop("bus", None)

                    map_to = answer_mapping["map_to"]
                    map_answer = answer_mapping["map_answer"]

                    if map_to == TYPE_COMPONENT:
                        # if question_id == "3":
                        #     import pdb;pdb.set_trace()
                        # TODO here for bus handling
                        components_to_add = []
                        # TODO here make this check independent of question id
                        if (
                            question_id.startswith("4_")
                            and question_id.endswith(".1")
                            and isinstance(answer, (int, float))
                        ):
                            # treat any float as salinity selected
                            components_to_add.extend(map_answer["salinity_selected"])
                            other_answers = []

                        else:
                            # Align answer structure: Should always be of type "list" to match component mapping
                            answer = [answer] if not isinstance(answer, list) else answer

                            # loop over the answers provided and add components to the energy system if the answer finds
                            # itself within the survey answer mapping. If the answer
                            other_answers = []
                            for a in answer:
                                if a in map_answer:
                                    components_to_add.extend(map_answer[a])

                                else:
                                    other_answers.append(str(a))

                        # temporary error solving trick for parallel components
                        for component in components_to_add:
                            if isinstance(component, list):
                                # parallel components: add each one
                                for subcomponent in component:
                                    self.ensure_component(subcomponent)
                            else:
                                # single sequential component
                                self.ensure_component(component)

                        # self.components.update({(component,component): {} for component in components_to_add})
                        self.wished_components[question_id] = other_answers

                    elif map_to == TYPE_COMPONENT_ATTRIBUTE:
                        if question_id in self.subq_mapping:
                            # Obtain parent question_id and answer to link attribute to its component
                            parent_qid, parent_answer = self.subq_mapping[question_id]

                            # Align answer structure: Should always be single item to match attribute mapping
                            answer = answer[0] if isinstance(answer, list) else answer
                            # print(map_answer)
                            # import pdb;pdb.set_trace()

                            # example for opt 2: question 4.2, map_answer = {'water_metals': ['Arsenic', 'Lead', 'Mercury', 'Cadmium', 'Iron']}
                            # TODO: Modify these questions to be TYPE_COMPONENT formatted according to option 1

                            ((attribute_name, attribute_type),) = map_answer.items()
                            attribute_val = type_check[attribute_type](answer)
                            try:
                                target_components = self.mapping[parent_qid]["map_answer"][parent_answer]

                                for target_component in target_components:
                                    comp = self.ensure_component(target_component)
                                    if comp is None:
                                        continue

                                    # If an already existing attribute of numeric type shall be assigned again, take the maximum value
                                    if attribute_name in comp:
                                        if isinstance(attribute_val, (int, float)):
                                            comp[attribute_name] = max(comp[attribute_name], attribute_val)
                                        else:
                                            comp[attribute_name] = attribute_val
                                    else:
                                        comp[attribute_name] = attribute_val
                            except:
                                print(f"There is a problem with question {question_id}")
                                # import pdb;pdb.set_trace()

                        else:
                            # TODO: Check if TYPE_COMPONENT_ATTRIBUTE questions are always subquestions of a TYPE_COMPONENT question
                            pass

                    elif map_to == TYPE_NO_MAP:
                        # Don't know what to do with this
                        pass
                    elif map_to == TYPE_OTHER:
                        self.wished_components[question_id] = answer
                    else:
                        print(f"Question {question_id} has unexpected key {map_to} that can't be mapped.")
                        pass

    def add_components(self):
        # TODO: does not add components that are not in AVAILABLE_COMPONENTS (component: "other")
        """
        Add all components and their corresponding attributes from the survey to the corresponding csv files. If a
        folder for the scenario doesn't exist, it will be created. If it does, the components and corresponding
        attributes will be updated
        """

        dp = self.scenario_datapackage
        dp_ref = self.reference_datapackage
        for component_key in self.components:
            component_type, component_name = component_key
            if component_type in AVAILABLE_COMPONENTS:
                # Load the resource from the reference datapackage
                resource = dp_ref.get_resource(AVAILABLE_COMPONENTS[component_type])
                df = pd.DataFrame.from_records(resource.read(keyed=True))
                df.set_index("name", drop=False, inplace=True)

                # Strip the component documentation columns
                selected_columns = [col for col in df.columns if col not in ["description"]]

                component_params = df.loc[component_type]
                component_params = component_params[selected_columns]
                # Edit the attributes in the csv file if they have been set in the survey
                component_params = self.update_component_attributes(component_key, component_params)
                ofname = os.path.join(self.scenario_component_folder, f"{AVAILABLE_COMPONENTS[component_type]}.csv")

                # Write or modify the component in the new datapackage
                if os.path.exists(ofname):
                    component_df = pd.read_csv(ofname, sep=";")
                    existing_records = component_df.name.tolist()
                    if component_params["name"] not in existing_records and component_name not in existing_records:
                        # If the component doesn't exist, add a row for the component
                        component_df = pd.concat([component_df, component_params.to_frame().T])
                    else:
                        # If the component already exists, only update the attributes
                        if component_params["name"] in existing_records:
                            component_name = component_params["name"]
                        component_df.set_index("name", drop=False, inplace=True)
                        component_params = component_df.loc[component_name].copy()
                        component_df.loc[component_name] = self.update_component_attributes(
                            component_key, component_params
                        )
                else:
                    # Copy package metadata
                    descriptor = deepcopy(resource.descriptor)
                    selected_fields = []
                    for f in descriptor["schema"]["fields"]:
                        if f["name"] in selected_columns + ["name"]:
                            selected_fields.append(f)
                    descriptor["schema"]["fields"] = selected_fields
                    dp.add_resource(descriptor)
                    dp.commit()

                    component_df = component_params.to_frame().T

                # Save the components back to the csv file
                component_df[selected_columns].to_csv(ofname, index=False, sep=";")

            else:
                logging.warning(
                    f"The component {component_key} is not in the available component list {', '.join([comp for comp in AVAILABLE_COMPONENTS])}"
                )

        dp.save(os.path.join(self.scenario_folder, "datapackage.json"))

    def update_component_attributes(self, component_key, component_params):
        """
        Edit the component attributes in the corresponding .csv file based on the component attributes set in
        self.components.
        :param component_key: tuple containing the component type and component name
        :param component_params: DataSeries object containing the .csv row of parameters for the corresponding component
        :returns: component_params DataSeries updated according to attributes in self.components[component]
        """
        for attr in self.components[component_key]:
            try:
                component_params.loc[attr] = self.components[component_key][attr]
            except KeyError:
                logging.warning(f"Attribute {attr} was not found for {component_key}")

        if "name" not in self.components[component_key]:
            # make sure the name provided in the component_key is used instead of the name of component library
            if component_key[0] != component_key[1]:
                component_params["name"] = component_key[1]
        return component_params

    def add_single_component(self, component_type, component_name=None, component_attrs=None):
        if component_attrs is None:
            component_attrs = {}

        if not isinstance(component_attrs, dict):
            logging.warning(
                f"The component attributes '{component_attrs}' of component '{component_type}' must be of type 'dict'! Will be ignored..."
            )
            component_attrs = {}

        if component_name is None:
            component_key = (component_type, component_type)
        elif isinstance(component_name, tuple):
            component_key = component_name
        elif isinstance(component_name, str):
            component_key = (component_type, component_name)
        else:
            logging.warning(
                f"The component name '{component_name}' of component type '{component_type}' is neither a string nor a tuple"
            )

        self.components.update({component_key: component_attrs})
        return component_key

    def add_sequences(self, custom_timeseries=None):
        """
        Looks for the column "profile" within the elements .csv files. If existing, creates a *element*_profile file
        in the sequences folder. If no custom timeseries are provided, default or previously retrieved timeseries (e.g.
        for renewable energy output) will be used.
        :param custom_timeseries: DataFrame with datetime index and elements as columns (should be uploaded as .csv or .xlsx
        """
        dp_ref = self.reference_datapackage
        dp = self.scenario_datapackage

        if not os.path.exists(self.scenario_component_folder):
            logging.warning("No components found to add timeseries. Please add components to the system first.")

        else:
            profiles_to_add = []
            for res in dp.resources:
                x = res.name
                if "/elements/" in res.descriptor["path"]:
                    try:
                        resource_data = pd.DataFrame.from_records(res.read(keyed=True))
                    except tableschema.exceptions.CastError as err:
                        if err.errors:
                            logging.error(
                                f"The resource {res.name} has the following casting errors: {','.join([str(e) for e in err.errors])}"
                            )
                        else:
                            logging.error(f"The resource {res.name} has the following casting error: {err}")
                        resource_data = pd.DataFrame()
                    for fk in res.descriptor["schema"]["foreignKeys"]:
                        fk_target = fk["reference"]["resource"]
                        if fk_target != "bus":
                            target_res = dp_ref.get_resource(fk_target)
                            sequence_headers = [
                                f"{f['name']}" for f in target_res.descriptor["schema"].get("fields", [])
                            ]
                            col_name = fk["fields"]
                            if col_name in resource_data.columns:
                                profile_names = resource_data[col_name].values.tolist()
                                # check the bus names are listed in the component library
                                for profile_name in profile_names:
                                    if profile_name not in sequence_headers:
                                        logging.warning(
                                            f"In the column '{col_name}' of the resource '{res.name}' the profile {profile_name} is listed, however it is missing from the component library resource in data/sequences path"
                                        )
                                    else:
                                        if profile_name not in profiles_to_add:
                                            profiles_to_add.append(profile_name)
                            else:
                                logging.error(
                                    f"Column '{col_name}' missing from resource '{res.name}' although it is listed as foreignKey"
                                )

            if len(profiles_to_add) == 0:
                print(
                    f"No profiles listed within the component for the '{self.scenario_folder.split(os.sep)[-1]}' datapage. If you think it is an error, double check the foreign keys"
                )

            # Add cf-aware-profile which should always be present
            profiles_to_add.append("cf-aware-profile")

            # Get processed weather data, demand data and waste data
            weather_df = self.process_weather_data
            demand_df = self.demand_data
            waste_df = self.waste_data

            # Use waste data (static helper file) as measurement for profiles' length
            profiles_len = len(waste_df)
            if len(weather_df) != profiles_len:
                logging.warning(f"Length issue with weather data.")
            if len(demand_df) != profiles_len:
                logging.warning(f"Length issue with demand data.")

            # Create DF for the scenario profiles and match index with profiles length
            scen_profiles_df = pd.DataFrame(columns=profiles_to_add, index=range(profiles_len))

            # Add timeindex column in right format and length
            # TODO: This is still a bit dirty, timeinfo could be retrieved from Timeseries.objects
            #  but it gets lost during get_renewables_output
            # database weather data is for 2022
            scen_profiles_year = 2022
            timeindex = pd.date_range(start=f"{scen_profiles_year}-01-01", periods=profiles_len, freq="h", tz="UTC")
            scen_profiles_df["timeindex"] = timeindex.strftime("%Y-%m-%dT%H:%M:%SZ")

            # Get blueprint profile names from component library resource for mapping, copy descriptor to adapt metadata later
            profile_map = dp_ref.get_resource("profiles")
            descriptor = deepcopy(profile_map.descriptor)
            profile_map_df = pd.DataFrame.from_records(profile_map.read(keyed=True))

            # Compare with profiles from library profiles csv and in case of a match, populate with data from weather df
            for profile in profiles_to_add:
                if profile in profile_map_df.columns:
                    matching_col = str(profile_map_df[profile].iloc[0])
                    if matching_col in weather_df.columns:
                        scen_profiles_df[profile] = weather_df[matching_col].reindex(range(profiles_len)).values
                    elif matching_col in demand_df.columns:
                        scen_profiles_df[profile] = demand_df[matching_col].reindex(range(profiles_len)).values
                    elif matching_col in waste_df.columns:
                        scen_profiles_df[profile] = waste_df[matching_col].reindex(range(profiles_len)).values
                    else:
                        logging.warning(
                            f"Profile '{profile}' is not in the available data. A dummy profile (series of 1) will be used."
                        )
                        scen_profiles_df[profile] = pd.Series([1] * profiles_len)
                else:
                    logging.warning(
                        f"Profile '{profile}' is not in the profile library. A dummy profile (series of 1) will be used."
                    )
                    scen_profiles_df[profile] = pd.Series([1] * profiles_len)

            # Add profiles.csv to datapackage
            ofname = os.path.join(self.scenario_folder, "data", "sequences", "profiles.csv")
            scen_profiles_df.to_csv(ofname, index=False, sep=";")

            # The order of fields in datapackage.json has to match order of column names in profiles.csv
            selected_fields = []
            for profile in scen_profiles_df.columns:
                for f in descriptor["schema"]["fields"]:
                    if profile == f["name"]:
                        f["type"] = "number"
                        selected_fields.append(f)
                # TODO: in the future, timeindex should be part of profiles in dp_ref...nevertheless,
                #    as long as it is called "timeindex", this code will work as intended
                if profile == "timeindex":
                    selected_fields.append({"name": "timeindex", "type": "datetime", "format": "default"})
            descriptor["schema"]["fields"] = selected_fields
            dp.add_resource(descriptor)
            dp.commit()

            dp.save(os.path.join(self.scenario_folder, "datapackage.json"))

        # TODO check the foreign keys between timeseries and component attributes are valid
        # i.e. that each of the component attribute value correspond to a timeseries header

    def add_single_bus(self, name, balanced=True, carrier=""):
        # Check if bus is in component_lib, if not: Add to additional busses so that it will be ignored later
        dp_ref = self.reference_datapackage
        bus_ref = self.get_single_component_from_datapackage(dp=dp_ref, resource_name="bus", component_name=name)
        if bus_ref.empty:
            self.additional_busses.append(name)

        ofname = os.path.join(self.scenario_component_folder, "bus.csv")
        bus = pd.Series({"name": name, "type": "bus", "balanced": balanced, "carrier": carrier}).to_frame().T
        # Write or modify the bus in the new datapackage
        if os.path.exists(ofname):
            busses_df = pd.read_csv(ofname, sep=";")
            existing_records = busses_df.name.tolist()
            if name not in existing_records:
                # If the bus doesn't exist, add a row for it
                busses_df = pd.concat([busses_df, bus])
            else:
                # If the bus already exists, replace it
                busses_df.set_index("name", drop=False, inplace=True)
                try:
                    # TODO: ask chatgpt about the indexing error...
                    busses_df.loc[name] = bus
                except Exception:
                    # import pdb
                    # pdb.set_trace()
                    pass
        else:
            busses_df = bus
        # Save the components back to the csv file
        busses_df.to_csv(ofname, index=False, sep=";")

    def add_buses(self):
        """
        Adds a bus.csv file to elements containing all the necessary buses. Looks through existing components to check
        which buses should be in the system.
        """
        scenario_component_folder = self.scenario_component_folder

        dp_ref = self.reference_datapackage
        ref_buses = dp_ref.get_resource("bus")
        df_ref_buses = pd.DataFrame.from_records(ref_buses.read(keyed=True))

        dp = self.scenario_datapackage

        if not os.path.exists(scenario_component_folder):
            logging.warning("No components found to infer buses. Please add components to the system first.")

        else:
            buses_to_add = []
            for res in dp.resources:
                if ("/elements/" in res.descriptor["path"]) and res.name != "bus":
                    try:
                        resource_data = pd.DataFrame.from_records(res.read(keyed=True))
                    except tableschema.exceptions.CastError as err:
                        if err.errors:
                            logging.error(
                                f"The resource {res.name} has the following casting errors: {','.join([str(e) for e in err.errors])}"
                            )
                        else:
                            logging.error(f"The resource {res.name} has the following casting error: {err}")
                        resource_data = pd.DataFrame()
                    for fk in res.descriptor["schema"]["foreignKeys"]:
                        if fk["reference"]["resource"] == "bus":
                            col_name = fk["fields"]
                            if col_name in resource_data.columns:
                                bus_names = resource_data[col_name].values.tolist()
                                # check the bus names are listed in the component library
                                for bus_name in bus_names:
                                    if bus_name not in df_ref_buses.name.values:
                                        if bus_name not in self.additional_busses:
                                            raise KeyError(
                                                f"In the column '{col_name}' of the resource '{res.name}' the bus {bus_name} is listed, however it is missing from the component library resource 'bus.csv'"
                                            )

                                buses_to_add.extend(bus_names)
                            else:
                                logging.error(
                                    f"Add buses: column '{col_name}' missing from resource '{res.name}' although it is listed as foreignKey"
                                )

            # Convert to set to keep only unique values (add each bus once)
            buses_to_add = list(set(buses_to_add))

            df_buses = []

            for bus_name in buses_to_add:
                lines = df_ref_buses.loc[df_ref_buses.name == bus_name]
                df_buses.append(lines)
                logging.info(f"Added bus {bus_name} to the '{self.scenario_folder.split(os.sep)[-1]}' datapage")

            if len(buses_to_add) == 0:
                print(
                    f"No buses listed within the component for the '{self.scenario_folder.split(os.sep)[-1]}' datapage. This is likely because the foreign keys are missing from the component library's datapackage.json file."
                )

            ofname = os.path.join(scenario_component_folder, "bus.csv")

            # Write or modify the bus in the new datapackage
            if os.path.exists(ofname):
                busses_df = pd.read_csv(ofname, sep=";")
                existing_records = busses_df.name.tolist()
                if df_buses:
                    new_buses = [
                        bus for bus in df_buses if not bus.empty and bus["name"].iloc[0] not in existing_records
                    ]
                    if new_buses:
                        busses_df = pd.concat([busses_df] + new_buses, ignore_index=True)
            else:
                if df_buses:
                    busses_df = pd.concat(df_buses, ignore_index=True)
            # Save the components back to the csv file
            busses_df.to_csv(ofname, index=False, sep=";")

            # Update dp.json: Add resource "bus" if there are busses
            if not busses_df.empty:
                resource = dp_ref.get_resource("bus")
                descriptor = deepcopy(resource.descriptor)
                dp.add_resource(descriptor)

                dp.commit()
                dp.save(os.path.join(self.scenario_folder, "datapackage.json"))


if __name__ == "__main__":
    repo_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scenarios")
    # create_scenario_from_survey_data({}, "test_scenario", repo_path)

    scen_id = 19

    with open(os.path.join(project_dir, "app", f"scenario_{scen_id}_survey_answers.json"), "r") as fp:
        survey_answers = json.load(fp)

    scenario = WEFEConfigurator(scen_id=scen_id, overwrite=False)

    # Controls whether the water treatment system is simplified.
    # True  -> run the simplification function and use the simplified setup.
    # False -> skip simplification and keep the full treatment trains.
    run_water_simplification = True  # default

    # Parse the survey to add components to a list
    scenario.process_survey(survey_answers)

    # Add a load.csv component based on demand data from WEFEDemand
    scenario.process_demand()

    # scenario.water_systems_postprocessing(survey_answers)
    # scenario.waste_water_systems_postprocessing(survey_answers)

    # scenario.crop_systems_postprocessing()

    # adding the component to the datapackge from the component library based on the list of component
    # to add we got from the survey
    scenario.add_components()
    scenario.add_buses()
    scenario.add_sequences()

    # Apply simplification only when the flag is enabled.
    if run_water_simplification:
        scenario.water_systems_simplification()
