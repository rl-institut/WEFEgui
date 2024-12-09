from django.db import models
from django.utils.translation import gettext_lazy as _
from projects.models import Timeseries, Project, Scenario, Asset, Bus, UseCase, Simulation
from projects.scenario_topology_helpers import assign_assets, assign_busses
from wefe.survey import SURVEY_QUESTIONS_CATEGORIES


# class Options(models.Model):
#     project = models.ForeignKey(Project, on_delete=models.CASCADE, blank=True, null=True)


class SurveyQuestion(models.Model):
    question_id = models.CharField(null=False, max_length=30, primary_key=True)
    question = models.TextField(null=False)
    subquestion_to = models.ForeignKey("self", null=True, on_delete=models.CASCADE)
    subquestion = models.TextField(null=True)
    possible_answers = models.TextField(null=True)
    multiple_answers = models.BooleanField(default=False)
    answer_type = models.CharField(null=False, max_length=8)
    description = models.TextField(null=False, default="")
    category = models.CharField(
        max_length=60,
        null=True,
        blank=False,
        choices=[(k, v) for k, v in SURVEY_QUESTIONS_CATEGORIES.items()],
    )

    @property
    def id(self):
        return self.question_id


class SurveyAnswer(models.Model):
    question = models.ForeignKey(SurveyQuestion, on_delete=models.CASCADE, null=True, blank=False)
    value = models.TextField(null=True)
    # TODO make this a ForeignKey
    scenario_id = models.IntegerField(null=False)


def copy_energy_system_from_usecase(usecase_name, scenario):
    """Given a scenario, copy the topology of the usecase"""
    # Filter the name of the project and the usecasename within this project
    usecase_scenario = Scenario.objects.get(project=UseCase.objects.get(name="cp_usecases"), name=usecase_name)
    dm = usecase_scenario.export()
    assets = dm.pop("assets")
    busses = dm.pop("busses")
    # delete pre-existing energy system
    qs_assets = Asset.objects.filter(scenario=scenario)
    qs_busses = Bus.objects.filter(scenario=scenario)
    if qs_busses.exists() or qs_assets.exists():
        qs_assets.delete()
        qs_busses.delete()
    # assign the assets and busses to the given scenario
    assign_assets(scenario, assets)
    assign_busses(scenario, busses)
