import json
import numpy as np
import requests
from django.templatetags.static import static
import logging

logger = logging.getLogger(__name__)

from epa.settings import KOBO_API_TOKEN, KOBO_API_URL
from projects.models import Project, Timeseries
from projects.services import RenewablesNinja


def help_icon(help_text=""):
    return "<a data-bs-toggle='tooltip' title='' data-bs-original-title='{}' data-bs-placement='right'><img style='height: 1.2rem;margin-left:.5rem' alt='info icon' src='{}'></a>".format(
        help_text, static("assets/icons/i_info.svg")
    )


def get_renewables_output(proj_id, raw=True):
    """
    Gets the PV and Wind potential for the site
    :param proj_id: Project ID
    :param raw: when True, returns raw weather data (direct irradiance/wind speed), False returns normalized electricity output
    """

    suffixes = {
        "pv": "irradiance_direct" if raw else "electricity",
        "wind": "wind_speed" if raw else "electricity",
    }

    project = Project.objects.get(id=proj_id)
    coordinates = {"lat": project.latitude, "lon": project.longitude}
    pv_ts, created = Timeseries.objects.get_or_create(name=f"pv_ts_{suffixes['pv']}", scenario=project.scenario)
    wind_ts, _ = Timeseries.objects.get_or_create(name=f"wind_ts_{suffixes['wind']}", scenario=project.scenario)

    # only checking for one because if one exists, both should exist
    if created is True:
        location = RenewablesNinja()
        location.get_pv_data(coordinates)
        location.get_wind_data(coordinates)

        for ts, name in zip([pv_ts, wind_ts], ["pv", "wind"]):
            data = location.data[name]
            try:
                ts.values = np.squeeze(data[suffixes[name]]).tolist()
            except KeyError:
                # For the case that data fetching from renewables.ninja did not work
                # TODO decide how to handle case and if to set default in RN.fetch_and_parse_data()
                return None, None
            ts.start_time = data.index[0]
            ts.end_time = data.index[-1]
            ts.time_step = 60
            ts.save()

    return pv_ts.values, wind_ts.values


class KoboHandler:
    base_survey_id = "aUu2e9DtM6mQmiZJnqSHCv"
    request_headers = {"Accept": "application/json", "Authorization": "Token " + KOBO_API_TOKEN}

    def __init__(self, project):
        """When the class is initialized, a survey is cloned from the base survey, deployed and the permissions
        are changed so that anonymous users can submit to the form. The web form url is returned when the form is
        deployed"""
        # TODO save these somewhere (maybe in Options) so that the survey stays assigned to the project
        # TODO only create a new survey if this project doesn't already have a survey assigned to it
        # project = Project.objects.get(pk=project_id)
        # if project.options.kobo_survey is None:
        self.project_survey_id = None
        self.project = project
        # self.project_survey_id = self.clone_form()
        # self.assign_permissions("add_submissions", "AnonymousUser")
        # self.assign_permissions("view_asset", "AnonymousUser")
        # self.project_survey_url = self.deploy_form()

    def request_data(self, survey_id):
        pass

    def get_survey_metadata(self, survey_id=None):
        # TODO might be useful depending on how we need the surveys and what we save about them
        if survey_id is None:
            survey_id = self.project_survey_id

        response = requests.get(f"{KOBO_API_URL}assets/{survey_id}/", headers=self.request_headers, timeout=60)

        return response

    def send_request(self, endpoint, payload):
        try:
            logger.info(f"Sending request to KoboToolbox API {endpoint}")
            response = requests.post(
                f"{KOBO_API_URL}{endpoint}", json=payload, headers=self.request_headers, timeout=60
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
            "permission": f"https://kf.kobotoolbox.org/api/v2/permissions/{permission_codename}/",
            "user": f"https://kf.kobotoolbox.org/api/v2/users/{username}/",
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
