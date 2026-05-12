import io
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.db.models import Q, F, Avg, Max
from django.forms import model_to_dict
from django.http import HttpResponse, JsonResponse
from django.shortcuts import *
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods
from django_plotly_dash import DjangoDash
from jsonview.decorators import json_view
from oemof_tabular_plugins.general import prepare_app

from business_model.forms import *
from business_model.models import *
from projects.constants import DONE, ERROR
from projects.forms import UploadFileForm, ProjectShareForm, ProjectRevokeForm, UseCaseForm
from projects.models import *
from projects.models.base_models import Timeseries
from projects.views import project_duplicate, project_delete

from wefe.exports import create_wefe_pdf_report
from wefe.forms import *
from wefe.helpers import *
from wefe.models import MOOWeights, SurveyAnswer, WEFESimulation, WEFE_SIM_APP
from wefe.requests import (
    fetch_wefe_simulation_results,
    wefedemand_simulation_request,
    wefesim_simulation_request,
)
from wefe.scenario_builder import WEFEConfigurator
from wefe.survey import SURVEY_CATEGORIES, SURVEY_QUESTIONS_CATEGORIES, get_survey_question_by_id
from oemof_tabular_plugins.datapackage import export_single_json

import logging

logger = logging.getLogger(__name__)


STEP_MAPPING = {
    "choose_location": 1,
    "resources": 2,
    "demand": 3,
    "system_layout": 4,
    "optimization_weighting": 5,
    "simulation": 6,
    "results": 7,
}

WEFE_STEP_VERBOSE = {
    "choose_location": _("Choose location"),
    "resources": _("Resources_mapping"),
    "demand": _("Demand assessment"),
    "system_layout": _("System layout"),
    "optimization_weighting": _("Multi-objective optimization"),
    "simulation": _("Simulation"),
    "results": _("Results"),
}

# sorts the step names based on the order defined in STEP_MAPPING (for ribbon)
WEFE_STEP_VERBOSE = [WEFE_STEP_VERBOSE[k] for k, v in sorted(STEP_MAPPING.items(), key=lambda x: x[1])]


@require_http_methods(["GET"])
def wefe_home(request):
    return render(request, "wefe/index.html")


###########################################################################################
# Steps
###########################################################################################


@login_required
@require_http_methods(["GET", "POST"])
def wefe_steps(request, proj_id, step_id=None):
    if step_id is None:
        return HttpResponseRedirect(reverse("wefe_steps", args=[proj_id, 1]))
    # import pdb;pdb.set_trace()
    return WEFE_STEPS[step_id - 1](request, proj_id, step_id)


@login_required
@require_http_methods(["GET", "POST"])
def wefe_choose_location(request, proj_id=None, step_id=STEP_MAPPING["choose_location"]):
    qs_project = Project.objects.filter(id=proj_id)

    proj_name = ""
    if qs_project.exists():
        project = qs_project.get()
        if (project.user != request.user) and (
            project.viewers.filter(user__email=request.user.email, share_rights="edit").exists() is False
        ):
            raise PermissionDenied

    else:
        project = None

    if request.method == "POST":
        if project is not None:
            form = ProjectForm(request.POST, instance=project)
            economic_data = EconomicProjectForm(request.POST, instance=project.economic_data)
        else:
            form = ProjectForm(request.POST)
            economic_data = EconomicProjectForm(request.POST)
        if form.is_valid() and economic_data.is_valid():
            # If the project location has changed, delete existing weather data
            if project is not None and any(field in form.changed_data for field in ["latitude", "longitude"]):
                pv_data_qs = Timeseries.objects.filter(name__contains="pv_ts_", scenario=project.scenario)
                wind_data_qs = Timeseries.objects.filter(name__contains="wind_ts_", scenario=project.scenario)

                for qs in [pv_data_qs, wind_data_qs]:
                    if qs.exists():
                        print(f"deleting timeseries {[q.name for q in qs]}")
                        qs.delete()

            economic_data = economic_data.save(commit=False)
            # set the initial values for discount and tax
            economic_data.tax = 0.075
            economic_data.save()

            project = form.save(user=request.user, commit=False)
            project.economic_data = economic_data
            project.save()

            # options, _ = Options.objects.get_or_create(project=project)
            # options.save()

            return HttpResponseRedirect(reverse("wefe_steps", args=[project.id, step_id + 1]))

    elif request.method == "GET":
        if project is not None:
            scenario = Scenario.objects.filter(project=project).last()
            form = ProjectForm(
                instance=project,
                initial={"start_date": scenario.start_date, "duration": project.economic_data.duration},
            )
            economic_data = EconomicProjectForm(instance=project.economic_data)
            # qs_options = Options.objects.filter(project=project)

        else:
            form = ProjectForm()
            economic_data = EconomicProjectForm()
    page_information = "Please input basic project information, such as name, location and duration. You can input geographical data by clicking on the desired project location on the map."

    if project is not None:
        proj_name = project.name
    return render(
        request,
        "wefe/steps/choose_location.html",
        {
            "form": form,
            "economic_data": economic_data,
            "proj_id": proj_id,
            "proj_name": proj_name,
            "step_id": step_id,
            "step_list": WEFE_STEP_VERBOSE,
            "page_information": page_information,
            "OPEN_METEO_URL": settings.OPEN_METEO_URL,
            "OPEN_TOPO_URL": settings.OPEN_TOPO_URL,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def wefe_resources(request, proj_id, step_id=STEP_MAPPING["resources"]):
    project = get_object_or_404(Project, id=proj_id)

    if (project.user != request.user) and (
        project.viewers.filter(user__email=request.user.email, share_rights="edit").exists() is False
    ):
        raise PermissionDenied

    scenario = project.scenario
    page_information = "Renewable energy potential for the selected site"
    context = {
        "proj_id": proj_id,
        "proj_name": project.name,
        "step_id": step_id,
        "step_list": WEFE_STEP_VERBOSE,
        "page_information": page_information,
    }

    if request.method == "GET":
        timeseries = get_renewables_output(proj_id, raw=False)

        timeseries_labels = {
            "ghi": "Global Horizontal irradiance [W/m²]",
            "t_air": "Temperature [°C]",
            "t_dew": "Dewpoint Temperature[°C]",
            "windspeed": "Windspeed at 100 meters [m/s]",
            "fsr": "Forecast Surface Roughness (m)",
            "tp": "Precipitation [mm]",
            "e": "Evapotranspiration [mm]",
            "cf_aware": "Water Scarcity Footprint Factor [dimensionless]",
        }
        context.update(
            {
                "timestamps": scenario.get_timestamps(json_format=True),
                "timeseries": json.dumps(timeseries),
                "timeseries_keys": timeseries_labels,
                "test": "test",
            }
        )

        return render(request, "wefe/steps/resources.html", context)

    if request.method == "POST":
        # TODO
        return HttpResponseRedirect(reverse("wefe_steps", args=[proj_id, step_id + 1]))


@login_required
@require_http_methods(["GET", "POST"])
def wefe_demand(request, proj_id, step_id=STEP_MAPPING["demand"]):
    project = get_object_or_404(Project, id=proj_id)

    if (project.user != request.user) and (
        project.viewers.filter(user__email=request.user.email, share_rights="edit").exists() is False
    ):
        raise PermissionDenied

    scenario = project.scenario
    # We use the existing infrastructure from MVS Simulation, adding app, which should be one of WEFEAPP_CHOICES
    simulation, _ = WEFESimulation.objects.get_or_create(scenario_id=scenario.id, app="wefedemand")

    page_information = "About demand"

    if request.method == "GET":
        context = {
            "proj_id": proj_id,
            "proj_name": project.name,
            "step_id": step_id,
            "sim_id": simulation.id,
            "simulation_status": simulation.status,
            "simulation_errors": simulation.errors,
            "step_list": WEFE_STEP_VERBOSE,
            "page_information": page_information,
        }

        if project.kobo_survey_id is not None:
            kobo = KoboHandler(project)
            counts = kobo.get_data_summary(kobo.project_survey_id)
            context.update(
                {"survey_id": project.kobo_survey_id, "survey_url": project.kobo_survey_url, "kobo_counts": counts}
            )

        return render(request, "wefe/steps/demand.html", context)

    if request.method == "POST":
        return HttpResponseRedirect(reverse("wefe_steps", args=[proj_id, step_id + 1]))
    return None


def request_wefedemand_simulation(request, proj_id=None):
    if not proj_id:
        answer = JsonResponse(
            {"status": "error", "error": "No project id provided"},
            status=500,
            content_type="application/json",
        )

    project = get_object_or_404(Project, pk=proj_id)
    scen_id = project.scenario.id
    default_survey = request.GET.get("default_survey", "false")
    default_survey = True if default_survey == "true" else False
    args = {}
    if default_survey:
        survey_id = "ay5RwDzEgUQn73E9it3wCB"
        args["id"] = [576013455, 576161268]
    else:
        survey_id = project.kobo_survey_id

    data = {"survey_id": survey_id, "args": args}
    results = wefedemand_simulation_request(data)
    if results is None:
        error_msg = "Could not communicate with the server."
        logger.error(error_msg)
        messages.error(request, error_msg)
        # TODO redirect to prefilled feedback form / bug form
        answer = JsonResponse(
            {"status": "error", "error": error_msg},
            status=407,
            content_type="application/json",
        )
    else:
        # delete existing simulation
        WEFESimulation.objects.filter(scenario_id=scen_id, app="wefedemand").delete()

        # Create empty Simulation model object
        simulation = WEFESimulation(start_date=datetime.now(), scenario_id=scen_id, app="wefedemand")

        simulation.mvs_token = results["id"] if results["id"] else None

        if "status" in results.keys() and (results["status"] == DONE or results["status"] == ERROR):
            simulation.status = results["status"]
            simulation.results = results["results"]
            simulation.end_date = datetime.now()
        else:  # PENDING
            simulation.status = results["status"]

        simulation.elapsed_seconds = (datetime.now() - simulation.start_date).seconds
        simulation.save()

        answer = JsonResponse({"msg": "Sent simulation request"})

    return answer


def get_wefedemand_data(request, proj_id):
    proj = get_object_or_404(Project, id=proj_id)
    ts_qs = Timeseries.objects.filter(scenario=proj.scenario, name__contains="ramp_demand")
    index = pd.date_range(start="2025-01-01 00:00:00", end="2025-12-31 23:00:00", freq="h")
    index = index.strftime("%Y-%m-%dT%H:%M:%S").tolist()

    data = {}
    data["index"] = json.dumps(index)

    if ts_qs.exists():
        for ts in ts_qs:
            data[ts.name.replace("_ramp_demand", "")] = ts.values

    return JsonResponse(data)


def request_wefesim_simulation(request, proj_id=None, default_datapackage="false"):
    if not proj_id:
        answer = JsonResponse(
            {"status": "error", "error": "No project id provided"},
            status=500,
            content_type="application/json",
        )

    project = get_object_or_404(Project, pk=proj_id)
    scen_id = project.scenario.id
    default_datapackage = True if default_datapackage == "true" else False

    if default_datapackage:
        with open(staticfiles_storage.path("wefe_configurator/default_dp.json")) as json_data:
            sim_data = json.load(json_data)
    else:
        qs = SurveyAnswer.objects.filter(scenario_id=scen_id)
        survey_answers = {}
        for ans in qs:
            survey_answers.update(ans.export(ignore_empty=True))

        wefe_conf = WEFEConfigurator(scen_id=scen_id, overwrite=False)

        wefe_conf.process_survey(survey_answers)
        wefe_conf.process_demand()
        wefe_conf.water_systems_postprocessing(survey_answers)
        wefe_conf.waste_water_systems_postprocessing(survey_answers)
        wefe_conf.add_components()
        wefe_conf.add_buses()
        wefe_conf.add_sequences()

        run_water_simplification = True  # default

        # Apply simplification only when the flag is enabled.
        if run_water_simplification:
            wefe_conf.water_systems_simplification()

        # Turn datapackage data and metadata into single json
        scenario_dir = Path(wefe_conf.scenario_folder)
        dp_export = export_single_json(scenario_dir, scenario_dir)

        # pass the sim data json and remove the temporary directory again
        with open(dp_export, "r", encoding="utf-8") as dp:
            sim_data = json.load(dp)

        # get rid of temp folder
        wefe_conf.cleanup()

        # Initialize parameters dict
        sim_data["parameters"] = {"moo_wf": None, "wacc": None}

        # Integrate moo weighting factors into simulation data parameters
        if hasattr(project.scenario, "mooweights"):
            weights = project.scenario.mooweights
            sim_data["parameters"]["moo_wf"] = {
                "wf_cost": weights.total_cost,
                "wf_ghg": weights.co2_emissions,
                "wf_lr": weights.land_requirements,
                "wf_wf": weights.water_footprint,
            }

        # Integrate economic parameters
        if hasattr(project.economic_data, "discount"):
            sim_data["parameters"]["wacc"] = project.economic_data.discount

    # Make simulation request
    results = wefesim_simulation_request(sim_data)
    if results is None:
        error_msg = "Could not communicate with the server."
        logger.error(error_msg)
        messages.error(request, error_msg)
        # TODO redirect to prefilled feedback form / bug form
        answer = JsonResponse(
            {"status": "error", "error": error_msg},
            status=407,
            content_type="application/json",
        )
    else:
        # delete existing simulation
        WEFESimulation.objects.filter(scenario_id=scen_id, app="wefesim").delete()

        # Create empty Simulation model object
        simulation = WEFESimulation(start_date=datetime.now(), scenario_id=scen_id, app="wefesim")
        # simulation.datapackage = (
        #     sim_data  # store datapackage for dash app...but this is jsonified and holds more data than needed
        # )

        simulation.mvs_token = results["id"] if results["id"] else None

        if "status" in results.keys() and (results["status"] == DONE or results["status"] == ERROR):
            simulation.status = results["status"]
            simulation.results = results["results"]
            simulation.end_date = datetime.now()
        else:  # PENDING
            simulation.status = results["status"]

        simulation.elapsed_seconds = (datetime.now() - simulation.start_date).seconds
        simulation.save()

        answer = HttpResponseRedirect(reverse("wefe_simulation", args=[proj_id]))

    return answer


def get_wefesim_data(request, proj_id):
    # TODO handle wefesim data
    pass


@login_required
@require_http_methods(["GET"])
def wefe_simulation_cancel(request, proj_id):
    project = get_object_or_404(Project, id=proj_id)
    scen_id = project.scenario.id
    if project.user == request.user:
        qs = WEFESimulation.objects.filter(scenario=scen_id, app="wefesim")
        if qs.exists():
            qs.delete()
    else:
        messages.error(
            request,
            _("You do not have the permission to reset a simulation on a project shared with you"),
        )

    return HttpResponseRedirect(reverse("wefe_simulation", args=[scen_id]))


def is_matrix_source(field):
    field_classes = field.widget.attrs.get("class")
    answer = False
    if field_classes is not None:
        if "matrix_source" in field_classes:
            answer = True
    return answer


@login_required
@require_http_methods(["GET", "POST"])
def wefe_system_layout(request, proj_id, step_id=STEP_MAPPING["system_layout"]):
    project = get_object_or_404(Project, id=proj_id)
    scen_id = project.scenario.id
    if request.method == "POST":
        form = SurveyQuestionForm(request.POST, qs=SurveyAnswer.objects.filter(scenario_id=scen_id))

        if form.is_valid():
            qs = SurveyAnswer.objects.filter(scenario_id=scen_id)
            with open(f"scenario_{scen_id}_survey_answers.json", "w") as fp:
                json.dump(form.cleaned_data, fp, indent=4)
            for criteria_num, value in form.cleaned_data.items():
                crit = qs.get(question_id=criteria_num.replace("criteria_", ""))
                crit.value = json.dumps(value) if not isinstance(value, str) else value
                crit.save(update_fields=["value"])

            return HttpResponseRedirect(reverse("wefe_steps", args=[proj_id, step_id + 1]))

    else:
        if scen_id is None:
            last_scenario_id = (
                SurveyAnswer.objects.all().values_list("scenario_id", flat=True).distinct().order_by().last()
            )
            if last_scenario_id is None:
                last_scenario_id = 0
            scenario_id = last_scenario_id + 1
            return HttpResponseRedirect(reverse("view_survey", args=[scenario_id]))
        else:
            scenario_id = scen_id

            # Check if answers already exists, if not create them
            qs_answer = SurveyAnswer.objects.filter(scenario_id=scenario_id)
            create_new_form = False
            if qs_answer.exists():
                try:
                    form = SurveyQuestionForm(qs=qs_answer)
                # Could be caused by outdated survey answers
                except ObjectDoesNotExist:
                    create_new_form = True
                    pass
            else:
                create_new_form = True

            if create_new_form:
                questions = SurveyQuestion.objects.all()
                print(questions)
                for question in questions:
                    # print(question)
                    answer_param = {}
                    answer_param["scenario_id"] = scenario_id
                    answer_param["question"] = question
                    new_answer = SurveyAnswer(**answer_param)
                    new_answer.save()
                qs_answer = SurveyAnswer.objects.filter(scenario_id=scenario_id)

                form = SurveyQuestionForm(qs=qs_answer)

    categories = [cat for cat in SURVEY_QUESTIONS_CATEGORIES.keys()]
    categories_map = []
    matrix_headers = {}
    matrix_labels = {}

    for field in form.fields:
        question_id = field.split("criteria_")[1]
        # TODO: could be done from models "category" attribute
        cat = SURVEY_CATEGORIES.get(question_id)
        # TODO: reassign cat after testing phase is over
        categories_map.append(cat)
        # TODO here one can know that the question
        if is_matrix_source(form.fields[field]):
            subs = []
            labels = []
            question = get_survey_question_by_id(SURVEY_STRUCTURE, question_id)
            for answer, subquestions in question["subquestion"].items():
                labels.append(answer)
                for sq_id in subquestions:
                    q_main_id = ".".join(sq_id.split(".")[:2])
                    subquestion = get_survey_question_by_id(SURVEY_STRUCTURE, sq_id)
                    # print(subquestion)
                    if subquestion.get("display_type", "") == "matrix":
                        if subquestion["question"] not in subs:
                            subs.append(subquestion["question"])
            matrix_headers[field] = subs
            matrix_labels[field] = labels

    page_information = "This survey will allow the user to build and simulate an energy system"

    # Check which categories have field errors, so we can display them as open in the accordeon and the user can see the issue
    faulty_fields = [k.replace("criteria_", "") for k in form.errors.keys()]
    faulty_fields_cat = [SURVEY_CATEGORIES.get(q) for q in faulty_fields]
    faulty_fields_cat = set(faulty_fields_cat)

    answer = render(
        request,
        "wefe/steps/survey_layout.html",
        {
            "form": form,
            "scen_id": scen_id,
            "categories_map": categories_map,
            "categories": categories,
            "categories_verbose": SURVEY_QUESTIONS_CATEGORIES,
            "faulty_fields_cat": faulty_fields_cat,
            "matrix_headers": matrix_headers,
            "matrix_labels": matrix_labels,
            "proj_id": proj_id,
            "proj_name": project.name,
            "step_id": step_id,
            "step_list": WEFE_STEP_VERBOSE,
            "page_information": page_information,
        },
    )

    return answer


@login_required
@require_http_methods(["GET", "POST"])
def wefe_optimization_weighting(request, proj_id, step_id=STEP_MAPPING["optimization_weighting"]):
    project = get_object_or_404(Project, id=proj_id)

    if (project.user != request.user) and (
        project.viewers.filter(user__email=request.user.email, share_rights="edit").exists() is False
    ):
        raise PermissionDenied

    scenario = project.scenario
    # TODO add a form for the MOO weighting here

    page_information = "About defining the weighting for the multi-objective optimization"
    context = {
        "proj_id": proj_id,
        "proj_name": project.name,
        "step_id": step_id,
        "step_list": WEFE_STEP_VERBOSE,
        "page_information": page_information,
    }

    if request.method == "GET":
        try:
            # weights already defined: get values
            weights = scenario.mooweights
            context["form"] = MOOForm(initial=vars(weights))
            context["custom_weights"] = weights.total_cost != 1
        except ObjectDoesNotExist:
            # no weights yet: use defaults
            context["form"] = MOOForm()
        return render(request, "wefe/steps/moo_setup.html", context)

    if request.method == "POST":
        form = MOOForm(request.POST)
        if not form.is_valid():
            context["form"] = form
            return render(request, "wefe/steps/moo_setup.html", context)

        # save form data (1 to 1 relation between scenario and weights)
        # weights might already exist -> maybe just update -> saving ModelForm does not work
        MOOWeights.objects.update_or_create(
            scenario=scenario,
            defaults=form.cleaned_data,
        )
        return HttpResponseRedirect(reverse("wefe_steps", args=[proj_id, step_id + 1]))


@login_required
@require_http_methods(["GET", "POST"])
def wefe_simulation(request, proj_id, step_id=STEP_MAPPING["simulation"]):
    project = get_object_or_404(Project, id=proj_id)

    if (project.user != request.user) and (
        project.viewers.filter(user__email=request.user.email, share_rights="edit").exists() is False
    ):
        raise PermissionDenied

    scenario = project.scenario

    page_information = "Here the simulation will be started"

    if request.method == "GET":
        html_template = "wefe/steps/simulation/no-status.html"
        context = {
            "proj_id": proj_id,
            "proj_name": project.name,
            "step_id": step_id,
            "step_list": WEFE_STEP_VERBOSE,
            "page_information": page_information,
            "WEFESIM_GET_URL": settings.WEFESIM_GET_URL,
        }

        qs = WEFESimulation.objects.filter(scenario=project.scenario, app="wefesim")

        if qs.exists():
            simulation = qs.first()

            if simulation.status == PENDING:
                fetch_wefe_simulation_results(simulation)

            context.update(
                {
                    "sim_id": simulation.id,
                    "simulation_status": simulation.status,
                    "secondsElapsed": simulation.elapsed_seconds,
                    "mvs_token": simulation.mvs_token,
                }
            )

            if simulation.status == ERROR:
                context.update({"simulation_error_msg": simulation.errors})
                html_template = "wefe/steps/simulation/error.html"
            elif simulation.status == PENDING:
                html_template = "wefe/steps/simulation/pending.html"
            elif simulation.status == DONE:
                html_template = "wefe/steps/simulation/success.html"

        else:
            print("no simulation existing")
        return render(request, html_template, context)
    return None


@login_required
@require_http_methods(["GET", "POST"])
def wefe_results(request, proj_id, step_id=STEP_MAPPING["results"]):
    project = get_object_or_404(Project, id=proj_id)

    if (project.user != request.user) and (
        project.viewers.filter(user__email=request.user.email, share_rights="edit").exists() is False
    ):
        raise PermissionDenied

    scenario = project.scenario
    simulation = WEFESimulation.objects.get(scenario=scenario, app=WEFE_SIM_APP)
    results = json.loads(simulation.results)["results"]
    dash_tables = restore_dash_tables(results["dash_tables"])

    tables = dash_tables["result_tables"]
    services = dash_tables["service_tables"]
    units = dash_tables["parameters_units"]
    verbose_names = dash_tables["verbose_names"]
    dash_app_name = f"results_dash_{proj_id}"
    app = DjangoDash(dash_app_name)

    df_results = df_from_split_multiindex(
        results["df_results"], index_names=["bus", "direction", "asset", "carrier", "facade_type"]
    )

    qs = SurveyAnswer.objects.filter(scenario_id=scenario.id)
    survey_answers = {}
    for ans in qs:
        survey_answers.update(ans.export(ignore_empty=True))

    wefe_conf = WEFEConfigurator(scen_id=scenario.id, overwrite=False)

    wefe_conf.process_survey(survey_answers)
    wefe_conf.process_demand()
    wefe_conf.water_systems_postprocessing(survey_answers)
    wefe_conf.waste_water_systems_postprocessing(survey_answers)
    wefe_conf.add_components()
    wefe_conf.add_buses()
    wefe_conf.add_sequences()

    run_water_simplification = True  # default

    # Apply simplification only when the flag is enabled.
    if run_water_simplification:
        wefe_conf.water_systems_simplification()

    prepare_app(
        app=app,
        dp_path=os.path.join(wefe_conf.scenario_folder, "datapackage.json"),
        results=df_results,
        tables=tables,
        services=services,
        units=units,
        label_map=verbose_names,
    )

    wefe_conf.cleanup()

    page_information = "Results page with report option"
    context = {
        "proj_id": proj_id,
        "proj_name": project.name,
        "step_id": step_id,
        "step_list": WEFE_STEP_VERBOSE,
        "page_information": page_information,
        "dash_app": dash_app_name,
    }

    if request.method == "GET":
        return render(request, "wefe/steps/results.html", context)

    if request.method == "POST":
        # TODO
        return HttpResponseRedirect(reverse("wefe_steps", args=[proj_id, step_id + 1]))


@login_required
@require_http_methods(["POST"])
def wefe_export_pdf(request, proj_id):
    import json as _json

    project = get_object_or_404(Project, id=proj_id)
    if (project.user != request.user) and (
        project.viewers.filter(user__email=request.user.email, share_rights="edit").exists() is False
    ):
        raise PermissionDenied

    scenario = project.scenario
    simulation = WEFESimulation.objects.get(scenario=scenario, app=WEFE_SIM_APP)
    results = _json.loads(simulation.results)["results"]
    dash_tables = restore_dash_tables(results["dash_tables"])

    tables = dash_tables["result_tables"]
    services = dash_tables["service_tables"]
    units = dash_tables["parameters_units"]
    verbose_names = dash_tables["verbose_names"]

    body = _json.loads(request.body)
    image_list = body.get("images", [])

    buffer = create_wefe_pdf_report(
        dp_path=os.path.join(COMPONENT_TEMPLATES_PATH, "datapackage.json"),
        project_name=project.name,
        tables=tables,
        services=services,
        units=units,
        image_list=image_list,
        label_map=verbose_names,
    )

    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="wefe_results.pdf"'
    return response


WEFE_STEPS = {
    "choose_location": wefe_choose_location,
    "resources": wefe_resources,
    "demand": wefe_demand,
    "system_layout": wefe_system_layout,
    "optimization_weighting": wefe_optimization_weighting,
    "simulation": wefe_simulation,
    "results": wefe_results,
}

# sorts the order in which the views are served in wefe_steps (defined in STEP_MAPPING)
WEFE_STEPS = [WEFE_STEPS[k] for k, v in sorted(STEP_MAPPING.items(), key=lambda x: x[1]) if k in WEFE_STEPS]


###########################################################################################
# Projects
###########################################################################################


@login_required
@require_http_methods(["GET"])
def projects_list_cpn(request, proj_id=None):
    combined_projects_list = (
        Project.objects.filter(Q(user=request.user) | Q(viewers__user__email=request.user.email))
        .distinct()
        .order_by("date_created")
        .reverse()
    )

    scenario_upload_form = UploadFileForm(labels=dict(name=_("New scenario name"), file=_("Scenario file")))
    project_upload_form = UploadFileForm(labels=dict(name=_("New project name"), file=_("Project file")))
    project_share_form = ProjectShareForm()
    project_revoke_form = ProjectRevokeForm(proj_id=proj_id)
    usecase_form = UseCaseForm(usecase_qs=UseCase.objects.all(), usecase_url=reverse("usecase_search"))

    return render(
        request,
        "wefe/project_display.html",
        {
            "project_list": combined_projects_list,
            "proj_id": proj_id,
            "scenario_upload_form": scenario_upload_form,
            "project_upload_form": project_upload_form,
            "project_share_form": project_share_form,
            "project_revoke_form": project_revoke_form,
            "usecase_form": usecase_form,
            "translated_text": {
                "showScenarioText": _("Show scenarios"),
                "hideScenarioText": _("Hide scenarios"),
            },
        },
    )


@login_required
@require_http_methods(["POST"])
def wefe_project_delete(request, proj_id):
    project_delete(request, proj_id)
    return HttpResponseRedirect(reverse("projects_list_cpn"))


@login_required
@require_http_methods(["POST"])
def wefe_project_duplicate(request, proj_id):
    """Duplicates the selected project along with its associated scenarios"""
    project = get_object_or_404(Project, pk=proj_id)
    answer = project_duplicate(request, proj_id)
    new_proj_id = answer.url.split("/")[-1]
    options, created = Options.objects.get_or_create(project__id=proj_id)
    if created is False:
        options.pk = None
        options.project = Project.objects.get(pk=new_proj_id)
        options.save()
    return HttpResponseRedirect(reverse("projects_list_cpn", args=[new_proj_id]))


@login_required
def ajax_generate_survey_link(request):
    if request.headers.get("Content-Type") == "application/json":
        proj_id = int(request.GET.get("proj_id"))
        project = get_object_or_404(Project, id=proj_id)
        kobo = KoboHandler(project)
        project_survey_id = kobo.clone_form(kobo.base_survey_id)
        project_survey_url = kobo.deploy_form(project_survey_id)
        kobo.assign_permissions("add_submissions", "AnonymousUser", survey_id=project_survey_id)
        kobo.assign_permissions("view_asset", "AnonymousUser", survey_id=project_survey_id)

        # update the project attributes
        project.kobo_survey_id = project_survey_id
        project.kobo_survey_url = project_survey_url
        project.save()
        return JsonResponse({"url": project_survey_url})
    return ConnectionError("Invalid request")


@login_required
def ajax_delete_survey(request):
    if request.headers.get("Content-Type") == "application/json":
        proj_id = int(request.GET.get("proj_id"))
        project = get_object_or_404(Project, id=proj_id)
        kobo = KoboHandler(project)
        try:
            kobo.delete_form(kobo.project_survey_id)
            project.kobo_survey_id = None
            project.kobo_survey_url = None
            project.save()
            return JsonResponse({"msg": "Deleted form"}, status=200)
        except:
            return JsonResponse({"msg": "There was an error deleting the form"}, status=400)
    return JsonResponse({"error": "Invalid request"}, status=400)


@login_required
def ajax_process_survey(request):
    pass


@json_view
@login_required
@require_http_methods(["GET"])
def fetch_simulation_results(request, sim_id):
    print(f"Fetching results for sim {sim_id}")
    simulation = get_object_or_404(WEFESimulation, id=sim_id)
    are_result_ready = fetch_wefe_simulation_results(simulation)
    errors = simulation.errors
    return JsonResponse(
        dict(areResultReady=are_result_ready, errors=errors),
        status=200,
        content_type="application/json",
    )
