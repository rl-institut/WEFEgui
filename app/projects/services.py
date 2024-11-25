import logging
import traceback

from concurrent.futures import ThreadPoolExecutor

import os
import requests
import pandas as pd
import numpy as np
import json
from django_q.models import Schedule

from django.contrib import messages
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from django_q.models import Schedule
# from exchangelib import (
#     Credentials,
#     Account,
#     Message,
#     Mailbox,
# )  # pylint: disable=import-error
from requests.exceptions import ConnectionError  # pylint: disable=import-error

import smtplib
import warnings
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from epa.settings import (
    EXCHANGE_ACCOUNT,
    EXCHANGE_SERVER,
    EXCHANGE_EMAIL,
    RECIPIENTS,
    EXCHANGE_PW,
    EMAIL_SUBJECT_PREFIX,
    TIME_ZONE,
    USE_EXCHANGE_EMAIL_BACKEND,
)
from plotly.offline import plot
from plotly.graph_objs import Scatter

from projects.constants import PENDING
from projects.models import Simulation
from projects.requests import fetch_mvs_simulation_results

logger = logging.getLogger(__name__)


# email account which will send the feedback emails
EXCHANGE_ACCOUNT = os.getenv("EXCHANGE_ACCOUNT", "dummy@dummy.com")
EXCHANGE_PW = os.getenv("EXCHANGE_PW", "dummypw")
EXCHANGE_EMAIL = os.getenv("EXCHANGE_EMAIL", "dummy@dummy.com")
EXCHANGE_SERVER = os.getenv("EXCHANGE_SERVER", "dummy.com")
# email addresses to which the feedback emails will be sent
RECIPIENTS = os.getenv("RECIPIENTS", "dummy@dummy.com,dummy2@dummy.com").split(",")

r"""Functions meant to be powered by Django-Q.

Those functions require Django-Q cluster to run along with Django Server.
To achieve this `python manage.py qcluster` command needs to be executed.

"""


def check_simulation_objects(**kwargs):
    pending_simulations = Simulation.objects.filter(status=PENDING)
    if pending_simulations.count() == 0:
        logger.debug(f"No pending simulation found. Deleting Scheduler.")
        Schedule.objects.all().delete()
    # fetch_mvs_simulation_results mostly waits for MVS API to respond, so no ProcessPool is required.
    with ThreadPoolExecutor() as pool:
        pool.map(fetch_mvs_simulation_results, pending_simulations)
    logger.debug(f"Finished round for checking Simulation objects status.")

    logger.debug(f"Finished round for checking Simulation objects status.")


def create_or_delete_simulation_scheduler(**kwargs):
    r"""Initialize a Django-Q Scheduler for all Simulation objects.

    If there are Simulation objects in the database, being in "PENDING" state
    a Scheduler is created to check periodically each Simulation (utilizes MVS API).
    If there is no Simulation is "PENDING" state the Scheduler object is deleted.

    Parameters
    ----------
    **kwargs : dict
        Possible future keyword arguments.

    Returns
    -------
    bool :
        True if Scheduler object is created or False otherwise.

    """
    mvs_token = kwargs.get("mvs_token", "")

    if Schedule.objects.count() == 0:
        logger.info(
            f"No Scheduler found. Creating a new Scheduler to check Simulation {mvs_token}."
        )
        schedule = Schedule.objects.create(
            name=f"djangoQ_Scheduler-{mvs_token}",
            func="projects.services.check_simulation_objects",
            # args='5',
            schedule_type=Schedule.MINUTES,
            minutes=1
            # kwargs={'test_arg': 1, 'test_arg2': "test"}
        )
        if schedule.id:
            logger.info(
                f"New Scheduler Created to track simulation {mvs_token} objects status."
            )
            return True
        else:
            logger.debug(f"Scheduler already exists for {mvs_token}. Skipping.")
            return False


def send_feedback_email(subject, body):

    # smtp_server = config.MAIL_HOST
    # smtp_port = config.MAIL_PORT
    # smtp_username = config.MAIL_ADDRESS
    # smtp_password = config.MAIL_PW
    # message = MIMEMultipart()
    # message["From"] = config.HEADER_ADDRESS
    # message["To"] = to_address if '@' in to_address else config.MAIL_ADDRESS_LOGGER
    # message["Subject"] = subject
    # message.attach(MIMEText(msg, "plain"))
    # with smtplib.SMTP(smtp_server, smtp_port) as server:
    #     server.starttls()
    #     try:
    #         server.login(smtp_username, smtp_password)
    #         server.sendmail(config.MAIL_ADDRESS, message["To"], message.as_string())
    #     except smtplib.SMTPAuthenticationError as e:
    #         print('\n{}\n{}'.format(e, config.MAIL_ADDRESS.replace('@', '')))
    #         warnings.warn(str(e), category=UserWarning)

    pass
    # tz = EWSTimeZone(TIME_ZONE)
    # try:
    #     credentials = Credentials(EXCHANGE_ACCOUNT, EXCHANGE_PW)
    #
    #     config = Configuration(server=EXCHANGE_SERVER, credentials=credentials)
    #
    #     account = Account(
    #         EXCHANGE_EMAIL,
    #         credentials=credentials,
    #         autodiscover=False,
    #         default_timezone=tz,
    #         config=config,
    #     )
    #     recipients = [Mailbox(email_address=recipient) for recipient in RECIPIENTS]
    #     mail = Message(
    #         account=account,
    #         folder=account.sent,
    #         subject=EMAIL_SUBJECT_PREFIX + subject,
    #         body=body,
    #         to_recipients=recipients,
    #     )
    #     mail.send_and_save()
    # except Exception as ex:
    #     logger.warning(
    #         f"Couldn't send feedback email. Exception raised: {traceback.format_exc()}."
    #     )
    #     raise ex


def send_email(*, to_email, subject, message):
    """Send E-mail via MS Exchange Server using credentials from env vars
    Parameters
    ----------
    to_email : :obj:`str`
        Target mail address
    subject : :obj:`str`
        Subject of mail
    message : :obj:`str`
        Message body of mail
    Returns
    -------
    :obj:`bool`
        Success status (True: successful)
    """
    prefixed_subject = EMAIL_SUBJECT_PREFIX + subject

    # if USE_EXCHANGE_EMAIL_BACKEND is True:
    #
    #     tz = EWSTimeZone(TIME_ZONE)
    #     credentials = Credentials(EXCHANGE_ACCOUNT, EXCHANGE_PW)
    #     config = Configuration(server=EXCHANGE_SERVER, credentials=credentials)
    #
    #     try:
    #         account = Account(
    #             EXCHANGE_EMAIL,
    #             credentials=credentials,
    #             autodiscover=False,
    #             default_timezone=tz,
    #             config=config,
    #         )
    #     except ConnectionError as err:
    #         err_msg = _("Form - connection error:") + f" {err}"
    #         logger.error(err_msg)
    #         return False
    #     except Exception as err:  # pylint: disable=broad-except
    #         err_msg = _("Form - other error:") + f" {err}"
    #         logger.error(err_msg)
    #         return False
    #
    #     recipients = [Mailbox(email_address=to_email)]
    #
    #     msg = Message(
    #         account=account,
    #         folder=account.sent,
    #         subject=prefixed_subject,
    #         body=message,
    #         to_recipients=recipients,
    #     )
    #
    #     try:
    #         msg.send_and_save()
    #         return True
    #     except Exception as err:  # pylint: disable=broad-except
    #         err_msg = _("Form - mail sending error:") + f" {err}"
    #         logger.error(err_msg)
    #         return False
    # elif USE_EXCHANGE_EMAIL_BACKEND is False:
    #     print(
    #         "\n",
    #         "--- No email is send ---",
    #         "\n\n",
    #         "To:",
    #         to_email,
    #         "\n\n",
    #         "Subject:",
    #         prefixed_subject,
    #         "\n\n",
    #         "Message:",
    #         message,
    #         "\n",
    #     )
    #     return True
    # else:
    #     raise ValueError(
    #         "Email backend not configured.",
    #         "USE_EXCHANGE_EMAIL_BACKEND must be boolean of either True or False.",
    #     )
    #     return False


def excuses_design_under_development(request, link=False):
    if link is False:
        msg = """This page is still under development. What you see is a design draft of how it should look like. If you have ideas or feedback about the design, you are welcome to submit it using the <a href='{url}'>feedback form</a>"""
    else:
        msg = """This website is still under development and not all buttons link to what they should yet. This is the case of the link or button you just clicked on. If you have ideas or feedback on how to improve the design, you are welcome to submit it using the <a href='{url}'>feedback form</a>"""

    url = reverse("user_feedback")
    messages.warning(request, _(mark_safe(msg.format(url=url))))


def get_selected_scenarios_in_cache(request, proj_id):
    """Given a request and the project id returns the list of selected scenarios"""
    if isinstance(proj_id, int):
        proj_id = str(proj_id)
    selected_scenarios_per_project = request.session.get("selected_scenarios", {})
    selected_scenario = selected_scenarios_per_project.get(proj_id, [])
    return [int(scen_id) for scen_id in selected_scenario]


class RenewableNinjas:
    token = os.environ["RN_API_TOKEN"]
    api_base = "https://www.renewables.ninja/api/"

    def __init__(self):
        self.s = requests.session()
        # Send token header with each request
        self.s.headers = {"Authorization": "Token " + self.token}
        self.data = []

    def get_pv_output(self, coordinates):
        ##
        # Get PV data
        ##

        url = self.api_base + "data/pv"

        args = {
            "lat": coordinates["lat"],
            "lon": coordinates["lon"],
            "date_from": "2019-01-01",
            "date_to": "2019-12-31",
            "dataset": "merra2",
            "capacity": 1.0,
            "system_loss": 0.1,
            "tracking": 0,
            "tilt": 35,
            "azim": 180,
            "format": "json",
        }

        r = self.s.get(url, params=args)

        # Parse JSON to get a pandas.DataFrame of data and dict of metadata
        parsed_response = json.loads(r.text)

        pv_data = pd.read_json(json.dumps(parsed_response["data"]), orient="index")
        metadata = parsed_response["metadata"]

        self.data = pv_data
        return

    def create_pv_graph(self):
        date_range = pd.Series(pd.date_range("2019-01-01", "2019-12-31"))
        daily_avg = [
            np.mean(self.data.loc[day.strftime("%Y-%m-%d")]) for day in date_range
        ]
        plot_div = plot(
            [Scatter(x=date_range, y=daily_avg, mode="lines")], output_type="div"
        )
        return plot_div
