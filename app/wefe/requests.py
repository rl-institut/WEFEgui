from datetime import datetime

import httpx as requests
import json

from epa.settings import PROXY_CONFIG, WEFEDEMAND_POST_URL, WEFEDEMAND_GET_URL, WEFESIM_POST_URL, WEFESIM_GET_URL
from projects.constants import DONE, PENDING, ERROR
import logging

from wefe.helpers import process_wefedemand_response, process_wefesim_response
from wefe.models import WEATHER_DATA_APP, WEFE_DEMAND_APP, WEFE_SIM_APP

logger = logging.getLogger(__name__)

WEFEAPP_URL_MAPPING = {
    WEATHER_DATA_APP: None,
    WEFE_DEMAND_APP: (WEFEDEMAND_GET_URL, WEFEDEMAND_POST_URL),
    WEFE_SIM_APP: (WEFESIM_GET_URL, WEFESIM_POST_URL),
}


def wefedemand_simulation_request(data: dict):
    headers = {"content-type": "application/json"}

    try:
        response = requests.post(
            WEFEDEMAND_POST_URL,
            json=data,
            headers=headers,
        )

        # If the response was successful, no Exception will be raised
        response.raise_for_status()
    except requests.HTTPError as http_err:
        logger.error(f"HTTP error occurred: {http_err}")
        return None

    except Exception as err:
        logger.error(f"Other error occurred: {err}")
        return None

    else:
        logger.info("The simulation was sent successfully to WEFEDEMAND_ API.")
        return json.loads(response.text)


def wefe_simulation_check_status(url, token):
    try:
        response = requests.get(url + token, proxies=PROXY_CONFIG, verify=False)
        response.raise_for_status()
    except requests.HTTPError as http_err:
        logger.error(f"HTTP error occurred: {http_err}")
        return None

    except Exception as err:
        logger.error(f"Other error occurred: {err}")
        return None

    else:
        logger.info("Success!")
        return json.loads(response.text)


def fetch_wefe_simulation_results(simulation):
    if simulation.status == PENDING:
        url = WEFEAPP_URL_MAPPING[simulation.app][0]
        response = wefe_simulation_check_status(url=url, token=simulation.mvs_token)
        try:
            simulation.status = response["status"]
            simulation.errors = (
                json.dumps(json.loads(response["results"])[ERROR]) if simulation.status == ERROR else None
            )
            if simulation.status == DONE:
                if simulation.app == WEFE_DEMAND_APP:
                    process_wefedemand_response(simulation, response["results"])
                elif simulation.app == WEFE_SIM_APP:
                    process_wefesim_response(simulation, response["results"])
                else:
                    msg = "Results processing is not set up for this app."
                    ValueError(msg)

                print(f"The simulation {simulation.id} is finished")
        except Exception as e:
            logger.warning(f"An error occurred: {e}")
            simulation.status = ERROR
            simulation.results = None

        simulation.elapsed_seconds = (datetime.now() - simulation.start_date).seconds
        simulation.end_date = datetime.now() if response["status"] in [ERROR, DONE] else None
        simulation.save()

    return simulation.status != PENDING


def wefesim_simulation_request(data: dict):
    headers = {"content-type": "application/json"}
    try:
        response = requests.post(WEFESIM_POST_URL, json=data, headers=headers, proxies=PROXY_CONFIG, timeout=10)

        # If the response was successful, no Exception will be raised
        response.raise_for_status()
    except requests.HTTPError as http_err:
        logger.error(f"HTTP error occurred: {http_err}")
        return None

    except Exception as err:
        logger.error(f"Other error occurred: {err}")
        return None

    else:
        logger.info("The simulation was sent successfully to WEFESIM API.")
        return json.loads(response.text)
