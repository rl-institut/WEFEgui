from django.urls import path, re_path
from .views import *

urlpatterns = [
    path("", wefe_home, name="wefe_home"),
    # steps
    path("<int:proj_id>/edit/step/<int:step_id>", wefe_steps, name="wefe_steps"),
    path("<int:proj_id>/edit/<int:community_id>/step/<int:step_id>", wefe_steps, name="wefe_steps"),
    path("new/scenario", wefe_choose_location, name="wefe_new_scenario"),
    path("projects/list", projects_list_cpn, name="projects_list_cpn"),
    path("projects/list/<int:proj_id>", projects_list_cpn, name="projects_list_cpn"),
    path("project/duplicate/<int:proj_id>", wefe_project_duplicate, name="wefe_project_duplicate"),
    path("project/delete/<int:proj_id>", wefe_project_delete, name="wefe_project_delete"),
    path("<int:proj_id>/edit/create", wefe_choose_location, name="wefe_scenario_create"),
    path("<int:proj_id>/edit/submit", wefe_choose_location, name="wefe_scenario_submit"),
    path("<int:proj_id>/survey", wefe_system_layout, name="view_survey_questions"),
    path("<int:proj_id>/submit/survey", wefe_system_layout, name="submit_survey"),
    path("<int:proj_id>/view/survey", wefe_system_layout, name="view_survey"),
]
