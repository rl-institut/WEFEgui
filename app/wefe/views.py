import io

from django.contrib.auth.decorators import login_required
import json
import logging
import pandas as pd
import os
import base64
import re
from django.http import JsonResponse
from jsonview.decorators import json_view
from django.utils.translation import gettext_lazy as _
from django.shortcuts import *
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from django.views.decorators.http import require_http_methods
from django.contrib import messages
from django.db.models import Q, F, Avg, Max
from epa.settings import MVS_GET_URL, MVS_LP_FILE_URL
from .forms import *
from .helpers import *
from business_model.forms import *
from projects.requests import fetch_mvs_simulation_results
from projects.models import *
from projects.views import project_duplicate, project_delete
from business_model.models import *
from cp_nigeria.models import ConsumerGroup
from cp_nigeria.helpers import ReportHandler
from projects.forms import UploadFileForm, ProjectShareForm, ProjectRevokeForm, UseCaseForm
from projects.services import RenewablesNinja
from projects.constants import DONE, PENDING, ERROR
from projects.views import request_mvs_simulation, simulation_cancel
from business_model.helpers import B_MODELS
from dashboard.models import KPIScalarResults, KPICostsMatrixResults, FancyResults
from dashboard.helpers import KPI_PARAMETERS
from .models import SurveyAnswer
from .survey import SURVEY_CATEGORIES, SURVEY_QUESTIONS_CATEGORIES

logger = logging.getLogger(__name__)


STEP_MAPPING = {
    "choose_location": 1,
    "resources": 2,
    "demand": 3,
    "economic_parameters": 4,
    "system_layout": 5,
    "optimization_weighting": 6,
    "simulation": 7,
    "results": 8,
}

WEFE_STEP_VERBOSE = {
    "choose_location": _("Choose location"),
    "resources": _("Resources_mapping"),
    "demand": _("Demand assessment"),
    "economic_parameters": _("Economic parameters"),
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
            economic_data.discount = 0.12
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
        pv_ts, wind_ts = get_renewables_output(proj_id, raw=True)
        context.update(
            {
                "timestamps": scenario.get_timestamps(json_format=True),
                "pv_ts": pv_ts,
                "wind_ts": wind_ts,
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

    page_information = "About demand"

    if request.method == "GET":
        context = {
            "proj_id": proj_id,
            "proj_name": project.name,
            "step_id": step_id,
            "step_list": WEFE_STEP_VERBOSE,
            "page_information": page_information,
        }

        if project.kobo_survey_id is not None:
            context.update({"survey_id": project.kobo_survey_id, "survey_url": project.kobo_survey_url})

        return render(request, "wefe/steps/demand.html", context)

    if request.method == "POST":
        # TODO
        return HttpResponseRedirect(reverse("wefe_steps", args=[proj_id, step_id + 1]))


@login_required
@require_http_methods(["GET", "POST"])
def wefe_economic_parameters(request, proj_id, step_id=STEP_MAPPING["economic_parameters"]):
    project = get_object_or_404(Project, id=proj_id)

    if (project.user != request.user) and (
        project.viewers.filter(user__email=request.user.email, share_rights="edit").exists() is False
    ):
        raise PermissionDenied

    scenario = project.scenario

    page_information = "About economic parameters"
    context = {
        "proj_id": proj_id,
        "proj_name": project.name,
        "step_id": step_id,
        "step_list": WEFE_STEP_VERBOSE,
        "page_information": page_information,
    }

    if request.method == "GET":
        return render(request, "wefe/steps/demand.html", context)

    if request.method == "POST":
        # TODO
        return HttpResponseRedirect(reverse("wefe_steps", args=[proj_id, step_id + 1]))


@login_required
@require_http_methods(["GET", "POST"])
def wefe_system_layout(request, proj_id, step_id=STEP_MAPPING["system_layout"]):
    project = get_object_or_404(Project, id=proj_id)
    scen_id = project.scenario.id
    if request.method == "POST":
        form = SurveyQuestionForm(request.POST, qs=SurveyAnswer.objects.filter(scenario_id=scen_id))

        if form.is_valid():
            qs = SurveyAnswer.objects.filter(scenario_id=scen_id)

            for criteria_num, value in form.cleaned_data.items():
                crit = qs.get(question_id=criteria_num.replace("criteria_", ""))
                crit.value = value
                crit.save(update_fields=["value"])

            answer = HttpResponseRedirect(reverse("view_survey", args=[scen_id]))
        else:
            # TODO
            import pdb

            pdb.set_trace()

    else:
        # TODO this is currently for testing
        if scen_id is None:
            scenario_id = 1
        else:
            scenario_id = scen_id

        # Check if answers already exists, if not create them
        qs = SurveyAnswer.objects.filter(scenario_id=scenario_id)
        if qs.exists() is False:
            questions = SurveyQuestion.objects.all()
            for question in questions:
                answer_param = {}
                answer_param["scenario_id"] = scenario_id
                answer_param["question"] = question
                new_answer = SurveyAnswer(**answer_param)
                new_answer.save()

        categories = [cat for cat in SURVEY_QUESTIONS_CATEGORIES.keys()]

        form = SurveyQuestionForm(qs=SurveyAnswer.objects.filter(scenario_id=scenario_id))

        categories_map = []
        for field in form.fields:
            question_id = field.split("criteria_")[1]
            # TODO: could be done from models "category" attribute
            cat = SURVEY_CATEGORIES[question_id]
            # TODO: reassign cat after testing phase is over
            categories_map.append("components")

        answer = render(
            request,
            "wefe/steps/survey_layout.html",
            {
                "form": form,
                "scen_id": scenario_id,
                "categories_map": categories_map,
                "categories": categories,
                "categories_verbose": SURVEY_QUESTIONS_CATEGORIES,
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

    page_information = "About defining the weighting for the multi-objective optimization"
    context = {
        "proj_id": proj_id,
        "proj_name": project.name,
        "step_id": step_id,
        "step_list": WEFE_STEP_VERBOSE,
        "page_information": page_information,
    }

    if request.method == "GET":
        return render(request, "wefe/steps/demand.html", context)

    if request.method == "POST":
        # TODO
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
    context = {
        "proj_id": proj_id,
        "proj_name": project.name,
        "step_id": step_id,
        "step_list": WEFE_STEP_VERBOSE,
        "page_information": page_information,
    }

    if request.method == "GET":
        return render(request, "wefe/steps/demand.html", context)

    if request.method == "POST":
        # TODO
        return HttpResponseRedirect(reverse("wefe_steps", args=[proj_id, step_id + 1]))


@login_required
@require_http_methods(["GET", "POST"])
def wefe_results(request, proj_id, step_id=STEP_MAPPING["results"]):
    project = get_object_or_404(Project, id=proj_id)

    if (project.user != request.user) and (
        project.viewers.filter(user__email=request.user.email, share_rights="edit").exists() is False
    ):
        raise PermissionDenied

    scenario = project.scenario

    page_information = "Results page with report option"
    context = {
        "proj_id": proj_id,
        "proj_name": project.name,
        "step_id": step_id,
        "step_list": WEFE_STEP_VERBOSE,
        "page_information": page_information,
    }

    if request.method == "GET":
        return render(request, "wefe/steps/demand.html", context)

    if request.method == "POST":
        # TODO
        return HttpResponseRedirect(reverse("wefe_steps", args=[proj_id, step_id + 1]))


WEFE_STEPS = {
    "choose_location": wefe_choose_location,
    "resources": wefe_resources,
    "demand": wefe_demand,
    "economic_parameters": wefe_economic_parameters,
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
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
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


@login_required
def ajax_process_survey(request):
    pass
