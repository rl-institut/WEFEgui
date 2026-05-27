import json
from datetime import datetime
from pathlib import Path

import numpy as np
import requests
from django.contrib.staticfiles.storage import staticfiles_storage
from django.shortcuts import get_object_or_404
from django.templatetags.static import static
import logging
import pandas as pd
import os

import datapackage as dp
import tableschema


logger = logging.getLogger(__name__)

from epa.settings import (
    KOBO_API_TOKEN,
    KOBO_API_URL,
    WEATHER_DATA_API_HOST,
    COMPONENT_TEMPLATES_PATH,
    COMPONENT_HELPERS_PATH,
)
from projects.models import Project, Timeseries


def convert_csvs_to_parquet(path=COMPONENT_HELPERS_PATH):
    for csv_path in Path(path).glob("*.csv"):
        parquet_path = csv_path.with_suffix(".parquet")
        df = pd.read_csv(csv_path)
        df.to_parquet(parquet_path, compression="snappy")
        print(f"Converted {csv_path} to {parquet_path}")


def help_icon(help_text=""):
    return "<a data-bs-toggle='tooltip' title='' data-bs-original-title='{}' data-bs-placement='right'><img style='height: 1.2rem;margin-left:.5rem' alt='info icon' src='{}'></a>".format(
        help_text, static("assets/icons/i_info.svg")
    )


def get_data(latitude=52.5200, longitude=13.4050, timeinfo=False):
    logger = logging.getLogger(__name__)
    session = requests.Session()

    headers = {"Referer": WEATHER_DATA_API_HOST + "wefe/"}
    # alternative to avoid payload:
    # url = WEATHER_DATA_API_HOST + f"wefe/?lat={latitude}&lng={longitude}"
    payload = {"latitude": latitude, "longitude": longitude}
    response = session.get(WEATHER_DATA_API_HOST + "wefe/", params=payload, headers=headers)
    if response.status_code == 200:
        data = response.json()
        df = pd.DataFrame(data["variables"])
        logger.info("The weather data API fetch worked successfully")

        if timeinfo:
            timeindex = data["time"]
    else:
        df = pd.DataFrame()
        logger.error(f"The weather data API fetch did not work: {response.reason} ({response.status_code})")
        timeindex = None

    if timeinfo:
        return df, timeindex
    else:
        return df


def get_renewables_output(proj_id, raw=True):
    """
    Gets the PV and Wind potential for the site
    :param proj_id: Project ID
    :param raw: when True, returns raw weather data (direct irradiance/wind speed), False returns normalized electricity output
    """

    project = Project.objects.get(id=proj_id)
    qs_ts = Timeseries.objects.filter(scenario=project.scenario, name__startswith="weather_data")
    if not qs_ts.exists():
        df, timeinfo = get_data(latitude=project.latitude, longitude=project.longitude, timeinfo=True)

        if not raw:
            used_cols = ["ghi", "t_air", "t_dew", "windspeed", "fsr", "tp", "e", "cf_aware"]
            df = df[used_cols]

        for col in df.columns:
            ts = Timeseries.objects.create(
                name=f"weather_data_{col}",
                scenario=project.scenario,
                values=df[col].values.tolist(),
                start_time=timeinfo["start"],
                end_time=timeinfo["end"],
                time_step=8760,
            )
            ts.save()
        qs_ts = Timeseries.objects.filter(scenario=project.scenario)
    collected_timeseries = {ts.name: ts.values for ts in qs_ts}
    return collected_timeseries


class KoboHandler:
    base_survey_id = "aKHX8DMDaQ6VAHuBzgSwCZ"
    request_headers = {"Accept": "application/json", "Authorization": "Token " + str(KOBO_API_TOKEN)}

    def __init__(self, project):
        """When the class is initialized, a survey is cloned from the base survey, deployed and the permissions
        are changed so that anonymous users can submit to the form. The web form url is returned when the form is
        deployed"""
        # TODO save these somewhere (maybe in Options) so that the survey stays assigned to the project
        # TODO only create a new survey if this project doesn't already have a survey assigned to it
        self.project_survey_id = project.kobo_survey_id
        self.project = project
        # self.project_survey_id = self.clone_form()
        # self.assign_permissions("add_submissions", "AnonymousUser")
        # self.assign_permissions("view_asset", "AnonymousUser")
        # self.project_survey_url = self.deploy_form()

    def request_data(self, survey_id=None):
        if survey_id is None:
            survey_id = self.project_survey_id

        response = requests.get(f"{KOBO_API_URL}/assets/{survey_id}/data", headers=self.request_headers, timeout=60)

        return response.json()

    def get_data_summary(self, survey_id=None):
        if survey_id is None:
            survey_id = self.project_survey_id

        kobo_json = self.request_data(survey_id)

        fields = [
            "respondent_local_aut",
            "respondent_service",
            "respondent_large_scale_farm",
            "respondent_business",
        ]

        counts = {field: 0 for field in fields} | {"respondent_unknown": 0}
        nr_responses = kobo_json.get("count")
        if nr_responses == 0:
            return counts
        else:
            for record in kobo_json.get("results", []):
                for field in fields:
                    key = f"G_0/{field}"
                    try:
                        if record.get(key) == "yes":
                            counts[field] += 1
                    except KeyError:
                        counts["respondent_unknown"] += 1

            counts["respondent_household"] = (
                nr_responses
                - counts["respondent_local_aut"]
                - counts["respondent_service"]
                - counts["respondent_large_scale_farm"]
                - counts["respondent_business"]
                - counts["respondent_unknown"]
            )

        return counts

    def get_survey_metadata(self, survey_id=None):
        # TODO might be useful depending on how we need the surveys and what we save about them
        if survey_id is None:
            survey_id = self.project_survey_id

        response = requests.get(f"{KOBO_API_URL}/assets/{survey_id}/", headers=self.request_headers, timeout=60)

        return response

    def send_request(self, endpoint, payload):
        if KOBO_API_URL is None:
            logger.error("KOBO_API_URL not set")
            return None
        if KOBO_API_TOKEN is None:
            logger.error("KOBO_API_TOKEN not set")
            return None
        try:
            logger.info(f"Sending request to KoboToolbox API {endpoint}")
            response = requests.post(
                f"{KOBO_API_URL}/{endpoint}", json=payload, headers=self.request_headers, timeout=60
            )
            response.raise_for_status()
            return response
        except Exception as err:
            logger.error(f"An error occurred: {err}")
            return None

    @staticmethod
    def get_enketo_url(response):
        response_json = json.loads(response.text)
        enketo_url = (
            response_json["asset"]["deployment__links"]["offline_url"]
            if "asset" in response_json
            else response_json["deployment__links"]["offline_url"]
        )
        return enketo_url

    def clone_form(self, survey_id=None):
        """Clones a KoboToolbox form. If no form is given, the base form given in the class will be cloned
        (corresponds to the basic IWI household questions survey). Returns the id of the newly created survey"""
        if survey_id is None:
            survey_id = self.base_survey_id

        payload = {"clone_from": survey_id, "name": f"WEFEDemand_proj{self.project.id}", "asset_type": "survey"}
        response = self.send_request(endpoint="assets/", payload=payload)
        if response is None:
            logger.warning("An error occurred while cloning the form")
            return
        else:
            new_survey_id = json.loads(response.text)["uid"]
            logger.info(f"Cloned demand survey to new survey with id {new_survey_id}.")
            self.project_survey_id = new_survey_id
            return new_survey_id

    def deploy_form(self, survey_id=None):
        """This call deploys the form. survey_id should be the id returned by clone_form. When the form is cloned,
        it is initially saved as a draft before being deployed. Returns the enketo url needed to fill
        out the survey"""

        if survey_id is None:
            survey_id = self.project_survey_id if self.project_survey_id is not None else self.clone_form()

        # this parameter makes sure that the form is deployed as active (otherwise it will default to archived)
        payload = {"active": True}
        response = self.send_request(endpoint=f"assets/{survey_id}/deployment/", payload=payload)

        if response is None:
            logger.warning("An error occurred while deploying the form")
            return
        else:
            enketo_url = self.get_enketo_url(response)
            logger.info(f"Successfully deployed survey with id {survey_id}. Survey available at {enketo_url}.")
            return enketo_url

    def assign_permissions(self, permission_codename, username, survey_id=None):
        """Assigns user permissions on a given form. For permissions without a KoboToolbox account, username should
        be 'AnonymousUser'. The basic permissions needed to anonymously submit to the form are view_asset and
        add_submissions"""
        if survey_id is None:
            survey_id = self.project_survey_id if self.project_survey_id is not None else self.clone_form()

        permission_list = [
            "change_asset",
            "view_asset",
            "manage_asset",
            "delete_asset",
            "change_submissions",
            "delete_submissions",
            "validate_submissions",
            "add_submissions",
            "view_submissions",
        ]

        if permission_codename not in permission_list:
            logger.warning(f"Permission doesn't exist. Available permission codenames are: '{permission_list}'")
            return None

        payload = {
            "permission": f"{KOBO_API_URL}/permissions/{permission_codename}/",
            "user": f"{KOBO_API_URL}/users/{username}/",
        }

        response = self.send_request(endpoint=f"assets/{survey_id}/permission-assignments/", payload=payload)
        if response is None:
            logger.warning(f"An error occurred while assigning {permission_codename} permission")
            return
        else:
            logger.info(f"Successfully assigned permission '{permission_codename}' to survey {survey_id}. ")
            return

    def delete_form(self, survey_id):
        payload = {
            "payload": {
                "asset_uids": [f"{survey_id}"],
                "action": "delete",
            }
        }

        response = self.send_request(endpoint=f"assets/bulk/", payload=payload)
        if response is None:
            logger.warning(f"An error occurred while deleting survey {survey_id}")
            return
        else:
            logger.info(f"Successfully deleted survey {survey_id}.")
            return


def process_wefedemand_response(simulation, wefedemand_response):
    project = simulation.scenario.project
    # delete old timeseries
    ts_qs = Timeseries.objects.filter(scenario=project.scenario, name__contains="ramp_demand")
    if ts_qs.exists():
        ts_qs.delete()

    for res in ["agg_mean", "agg_max"]:
        demand_dict = wefedemand_response[res]
        df = pd.DataFrame.from_dict(demand_dict)

        # create new timeseries
        for col in df:
            ts = Timeseries.objects.create(
                name=f"{col}_ramp_demand_{res}",
                scenario=project.scenario,
                values=df[col].values.tolist(),
                # start_time=timeinfo["start"],
                # end_time=timeinfo["end"],
                time_step=8760,
            )
            ts.save()
    return


def records_to_df(records):
    """
    Convert a list-of-records back to a DataFrame.
    Restores index if it was serialized via reset_index().
    """
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # restore common index patterns
    if "index" in df.columns:
        df = df.set_index("index")

    # restore named index
    if "kpi" in df.columns and df.columns.tolist() == ["kpi", "value"]:
        df = df.set_index("kpi")

    return df


def df_from_split_multiindex(payload, index_names=None):
    obj = json.loads(payload) if isinstance(payload, str) else payload

    # index
    idx = obj["index"]
    index = pd.MultiIndex.from_tuples([tuple(r) for r in idx], names=index_names)

    # columns
    cols = obj["columns"]

    # convert only datetime-like string columns to Timestamp labels
    cols = pd.Index(cols)
    datetimes = pd.to_datetime(cols, errors="coerce")  # parses ISO strings, leaves others as NaT
    time_cols = datetimes.notna()

    # keep original non-time labels; replace only time labels with Timestamps
    fixed_cols = pd.Index([dt if is_time else col for col, dt, is_time in zip(cols, datetimes, time_cols)])

    results_df = pd.DataFrame(obj["data"], index=index, columns=fixed_cols)

    # TODO figure out why this timestep is included instead
    results_df = results_df.drop(columns=[datetime(2023, 1, 1)])

    return results_df


def restore_dash_tables(json_data):
    """
    Reconstruct calculator.dash_tables from JSON object.
    """
    restored = {}

    for section, content in json_data.items():

        # e.g. result_tables, service_tables
        if isinstance(content, dict):
            restored[section] = {}

            for name, value in content.items():
                if isinstance(value, list):
                    restored[section][name] = records_to_df(value)
                else:
                    # parameters_units or other plain dicts
                    restored[section][name] = value
        else:
            restored[section] = content

    return restored


def process_wefesim_response(simulation, wefesim_response):
    results = json.loads(wefesim_response)["results"]
    simulation.results = wefesim_response
    simulation.save()
    # do not unpack tables here, as it is later done in the results view instead
    # data = {"df_results": results["df_results"], "dash_tables": restore_dash_tables(results["dash_tables"])}
    logger.info("The simulation results have been saved to the database")
    return


# Later direct imports without .json
# TODO update this mapping with the latest produced survey_answer_component_mapping.json
SURVEY_ANSWER_COMPONENT_MAPPING = {}
SUB_QUESTION_MAPPING = {}

if os.path.exists(
    staticfiles_storage.path("wefe_configurator/survey_helpers/survey_answer_component_mapping_in_use.json")
):
    with staticfiles_storage.open("wefe_configurator/survey_helpers/survey_answer_component_mapping_in_use.json") as fp:
        SURVEY_ANSWER_COMPONENT_MAPPING = json.load(fp)

if os.path.exists(staticfiles_storage.path("wefe_configurator/survey_helpers/sub_question_mapping.json")):
    with staticfiles_storage.open("wefe_configurator/survey_helpers/sub_question_mapping.json") as fp:
        SUB_QUESTION_MAPPING = json.load(fp)


def list_available_components():
    """browse all components in all csv files and link component name to csv file name"""

    path = COMPONENT_TEMPLATES_PATH
    dp_json = os.path.join(path, "datapackage.json")
    if os.path.exists(dp_json) is False:
        raise FileNotFoundError(
            "The component library datapackage is not there, please generate it using 'python validate_component_lib.py' "
        )
    else:
        p0 = dp.Package(dp_json)

    component_to_csv_name_mappping = {}
    for r in p0.resources:
        logging.info(r.name)
        if "/elements/" in r.descriptor["path"]:
            category = r.name
            try:
                resource_data = pd.DataFrame.from_records(r.read(keyed=True))
            except tableschema.exceptions.CastError as err:
                if err.errors:
                    logging.error(
                        f"The resource {category} has the following casting errors: {','.join([str(e) for e in err.errors])}"
                    )
                else:
                    logging.error(f"The resource {category} has the following casting error: {err}")
                resource_data = pd.DataFrame()

            if resource_data.empty is False:
                if len(resource_data.columns) == 1:
                    logging.warning(
                        f"The resource {category} has only one field detected, this is usually the case when there is a mismatch of number of values between the headers row and the data rows, please check your file."
                    )

                if category == "profiles":
                    import pdb

                    pdb.set_trace()

                for component_name in resource_data.name.values:
                    if component_name not in component_to_csv_name_mappping:
                        component_to_csv_name_mappping[component_name] = category
                    else:
                        raise ValueError(
                            f"The component {component_name} is listed under several categories: {component_to_csv_name_mappping[component_name]} and {category}"
                        )
            else:
                logging.warning(f"The resource {category} is empty")
    return component_to_csv_name_mappping


AVAILABLE_COMPONENTS = list_available_components()


def list_available_timeseries():
    """browse all components in all csv files and link component name to csv file name"""

    path = COMPONENT_TEMPLATES_PATH
    dp_json = os.path.join(path, "datapackage.json")
    if os.path.exists(dp_json) is False:
        raise FileNotFoundError(
            "The component library datapackage is not there, please generate it using 'python validate_component_lib.py' "
        )
    else:
        p0 = dp.Package(dp_json)

    sequence_to_csv_name_mappping = {}
    for r in p0.resources:
        logging.info(r.name)
        if "/sequences/" in r.descriptor["path"]:
            category = r.name
            try:
                resource_data = pd.DataFrame.from_records(r.read(keyed=True))
            except tableschema.exceptions.CastError as err:
                if err.errors:
                    logging.error(
                        f"The resource {category} has the following casting errors: {','.join([str(e) for e in err.errors])}"
                    )
                else:
                    logging.error(f"The resource {category} has the following casting error: {err}")
                resource_data = pd.DataFrame()

            if resource_data.empty is False:
                if len(resource_data.columns) == 1:
                    logging.warning(
                        f"The resource {category} has only one field detected, this is usually the case when there is a mismatch of number of values between the headers row and the data rows, please check your file."
                    )

                for component_name in resource_data.columns[1:]:
                    if component_name not in sequence_to_csv_name_mappping:
                        sequence_to_csv_name_mappping[component_name] = category
                    else:
                        raise ValueError(
                            f"The component {component_name} is listed under several categories: {sequence_to_csv_name_mappping[component_name]} and {category}"
                        )
            else:
                logging.warning(f"The resource {category} is empty")
    return sequence_to_csv_name_mappping


AVAILABLE_SEQUENCES = list_available_timeseries()


def create_components_list(survey_data):
    """Extrapolate the component of the energy system from the survey answers

    :param survey_data: dict with survey question code as key and the answer to the question as value
    :return:
    """
    component_list = []
    for question, survey_answer in survey_data.items():
        if question in SURVEY_ANSWER_COMPONENT_MAPPING:
            possible_answers = SURVEY_ANSWER_COMPONENT_MAPPING[question]
            if survey_answer in possible_answers:
                # TODO check that component is available in our database
                component_list.append(possible_answers[survey_answer])
        else:
            logging.info(f"Survey question '{question}' is not in the component mapping to build an energy system")
    return component_list


WATER_TREATMENT_TRAIN = {
    "main_list": [
        "intake_structure",
        "coarse_bar_screen",
        "fine_screen",
        "grit_chamber",
        "cartridge_filter",
        "simple_oxidation",
        "coagulation_flocculation",
        ["slow_sand_filter", "ceramic_filter", "biofiltration"],  # both series/parallel possible # membrane protection
        "microfiltration",
        "ultrafiltration",
        ["activated_carbon_filter", "adsorption"],  # both series/parallel possible # membrane protection
        "ion_exchange",
        "nanofiltration",
        ["electrodialysis", "reverse_osmosis"],  # both series/parallel possible
        "membrane_distillation",
        ["distillation", "boiling"],
        ["photocatalysis", "ozonation"],
        "biological_denitrification",
        ["slow_sand_filter", "ceramic_filter", "biofiltration"],  # both series/parallel possible # polishing
        ["uv_disinfection", "chlorination"],  # both series/parallel possible
        "activated_carbon_filter",  # polishing
    ],
    # Front-end treatment steps that prepare raw water for the main process.
    # These units mainly remove coarse solids, grit, particles, and unstable feed characteristics
    # so downstream treatment is protected from clogging, fouling, and performance loss.
    "pre_treatment": [
        "intake_structure",
        "coarse_bar_screen",
        "fine_screen",
        "grit_chamber",
        "cartridge_filter",
        "simple_oxidation",
        "coagulation_flocculation",
    ],
    # Main treatment steps that perform the primary water-quality transformation.
    # These units are responsible for the core removal of dissolved contaminants, salts,
    # pathogens, nutrients, organics, or other target pollutants.
    "core_treatment": [
        "slow_sand_filter",
        "ceramic_filter",
        "biofiltration",
        "microfiltration",
        "ultrafiltration",
        "activated_carbon_filter",
        "adsorption",
        "ion_exchange",
        "nanofiltration",
        "electrodialysis",
        "reverse_osmosis",
        "membrane_distillation",
        "distillation",
        "boiling",
        "photocatalysis",
        "ozonation",
        "biological_denitrification",
    ],
    # Final polishing and disinfection steps applied after the main treatment block.
    # These units are used to ensure microbiological safety and improve final water quality
    # before delivery to the drinking-water or service-water bus.
    "post_treatment": [
        "uv_disinfection",
        "chlorination",
    ],
    "pollutant_trains": {
        "drinking_water": {
            "decentralized": {
                "salinity": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter"],
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "membrane_distillation",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "arsenic": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "simple_oxidation",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "biofiltration"],
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    ["electrodialysis", "reverse_osmosis"],
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "lead": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter"],
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    "ion_exchange",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "mercury": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter"],
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    "ion_exchange",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "cadmium": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter"],
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    "ion_exchange",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "iron": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "simple_oxidation",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "biofiltration"],
                    "microfiltration",
                    "ultrafiltration",
                    "activated_carbon_filter",
                    ["uv_disinfection", "chlorination"],
                ],
                "pesticides": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter"],
                    "microfiltration",
                    "ultrafiltration",
                    ["photocatalysis", "ozonation"],
                    "biofiltration",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "pharmaceuticals": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter"],
                    "microfiltration",
                    "ultrafiltration",
                    ["photocatalysis", "ozonation"],  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "biofiltration",
                    "activated_carbon_filter",
                    "membrane_distillation",  # deviation till here
                    ["uv_disinfection", "chlorination"],
                ],
                "fertilizers": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter"],
                    "microfiltration",
                    "ultrafiltration",
                    "biological_denitrification",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "ion_exchange",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "adsorption",  # deviation till here
                    ["uv_disinfection", "chlorination"],
                ],
            },
            "centralized": {
                "salinity": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    "simple_oxidation",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter", "biofiltration"],
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "membrane_distillation",
                    "distillation",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "arsenic": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "simple_oxidation",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter", "biofiltration"],
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "membrane_distillation",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "lead": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    "simple_oxidation",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter", "biofiltration"],
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    "ion_exchange",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "membrane_distillation",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "mercury": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    ["simple_oxidation", "ozonation"],  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter", "biofiltration"],
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    "ion_exchange",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "membrane_distillation",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "cadmium": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    "simple_oxidation",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter", "biofiltration"],
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    "ion_exchange",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "membrane_distillation",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "iron": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "simple_oxidation",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "biofiltration"],
                    "microfiltration",
                    "ultrafiltration",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "pesticides": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    "simple_oxidation",
                    "coagulation_flocculation",
                    "microfiltration",
                    "ultrafiltration",
                    ["photocatalysis", "ozonation"],  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "biofiltration",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "activated_carbon_filter",
                    "membrane_distillation",  # deviation till here
                    ["uv_disinfection", "chlorination"],
                ],
                "pharmaceuticals": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    "microfiltration",
                    "ultrafiltration",
                    ["photocatalysis", "ozonation"],  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "biofiltration",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "activated_carbon_filter",
                    "membrane_distillation",  # deviation till here
                    ["uv_disinfection", "chlorination"],
                ],
                "fertilizers": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    "simple_oxidation",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "biofiltration"],
                    "microfiltration",
                    "ultrafiltration",
                    "biological_denitrification",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "ion_exchange",
                    "nanofiltration",
                    ["electrodialysis", "reverse_osmosis"],
                    "adsorption",
                    "membrane_distillation",  # deviation till here
                    ["uv_disinfection", "chlorination"],
                ],
            },
        },
        "service_water": {
            "decentralized": {
                "salinity": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter"],
                    "microfiltration",
                    "ultrafiltration",
                    ["nanofiltration", "electrodialysis"],
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "arsenic": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "simple_oxidation",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "cartridge_filter",
                    "coagulation_flocculation",
                    "adsorption",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "lead": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    "microfiltration",
                    "ultrafiltration",
                    "ion_exchange",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "mercury": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    ["uv_disinfection", "chlorination"],
                ],
                "cadmium": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    "microfiltration",
                    "ultrafiltration",
                    "ion_exchange",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "iron": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "simple_oxidation",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["cartridge_filter", "ceramic_filter"],
                    "coagulation_flocculation",
                    ["slow_sand_filter", "biofiltration"],
                    "activated_carbon_filter",
                    ["uv_disinfection", "chlorination"],
                ],
                "pesticides": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    "activated_carbon_filter",
                    "biofiltration",
                    ["uv_disinfection", "chlorination"],
                ],
                "pharmaceuticals": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    "activated_carbon_filter",
                    "biofiltration",
                    ["uv_disinfection", "chlorination"],
                ],
                "fertilizers": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    "microfiltration",
                    "ultrafiltration",
                    "biological_denitrification",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "ion_exchange",
                    "adsorption",  # deviation till here
                    ["uv_disinfection", "chlorination"],
                ],
            },
            "centralized": {
                "salinity": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    "microfiltration",
                    "ultrafiltration",
                    ["nanofiltration", "electrodialysis"],
                    "reverse_osmosis",
                    "membrane_distillation",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "arsenic": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "simple_oxidation",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "biofiltration"],
                    "adsorption",
                    ["nanofiltration", "reverse_osmosis"],
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "lead": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    "simple_oxidation",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter"],
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    "ion_exchange",
                    ["nanofiltration", "reverse_osmosis"],
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "mercury": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    ["simple_oxidation", "ozonation"],  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "coagulation_flocculation",
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    "ion_exchange",
                    ["reverse_osmosis", "membrane_distillation"],
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "cadmium": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "ceramic_filter"],
                    "microfiltration",
                    "ultrafiltration",
                    "adsorption",
                    "ion_exchange",
                    ["nanofiltration", "reverse_osmosis"],
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "iron": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "simple_oxidation",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "biofiltration"],
                    "microfiltration",
                    "ultrafiltration",
                    "activated_carbon_filter",
                    ["uv_disinfection", "chlorination"],
                ],
                "pesticides": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    "microfiltration",
                    "ultrafiltration",
                    ["photocatalysis", "ozonation"],
                    "biofiltration",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "pharmaceuticals": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    "microfiltration",
                    "ultrafiltration",
                    ["photocatalysis", "ozonation"],
                    "biofiltration",
                    "activated_carbon_filter",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    ["uv_disinfection", "chlorination"],
                ],
                "fertilizers": [
                    "intake_structure",
                    "coarse_bar_screen",
                    "fine_screen",
                    "grit_chamber",
                    "cartridge_filter",
                    "coagulation_flocculation",
                    ["slow_sand_filter", "biofiltration"],
                    "microfiltration",
                    "ultrafiltration",
                    "biological_denitrification",  # functional tradeoff,
                    # sequence deviates from master/main train to account for realistic engineering design
                    "ion_exchange",
                    ["nanofiltration", "adsorption"],  # deviation till here
                    ["uv_disinfection", "chlorination"],
                ],
            },
        },
    },
}
