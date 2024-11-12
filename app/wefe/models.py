from django.db import models
from django.utils.translation import gettext_lazy as _
from projects.models import Timeseries, Project, Scenario, Asset, Bus, UseCase, Simulation
from projects.scenario_topology_helpers import assign_assets, assign_busses


# class Options(models.Model):
#     project = models.ForeignKey(Project, on_delete=models.CASCADE, blank=True, null=True)


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
