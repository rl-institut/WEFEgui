import numpy as np
from django.templatetags.static import static
import logging

logger = logging.getLogger(__name__)

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
