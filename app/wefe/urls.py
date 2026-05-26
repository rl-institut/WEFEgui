from django.urls import path, re_path, include
from .views import *

urlpatterns = [
    path("django_plotly_dash/", include("django_plotly_dash.urls")),
    path("", wefe_home, name="wefe_home"),
    # steps
    path("<int:proj_id>/edit/step/<int:step_id>", wefe_steps, name="wefe_steps"),
    path("<int:proj_id>/edit/<int:community_id>/step/<int:step_id>", wefe_steps, name="wefe_steps"),
    path("new/scenario", wefe_choose_location, name="wefe_new_scenario"),
    path("projects/list", projects_list_cpn, name="projects_list_cpn"),
    path("projects/list/<int:proj_id>", projects_list_cpn, name="projects_list_cpn"),
    path("project/duplicate/<int:proj_id>", wefe_project_duplicate, name="wefe_project_duplicate"),
    path("project/delete/<int:proj_id>", wefe_project_delete, name="wefe_project_delete"),
    path(
        "project/wefesim_simulation/<int:proj_id>/<str:default_datapackage>",
        request_wefesim_simulation,
        name="request_wefesim_simulation",
    ),
    path("wefesim_simulation/<int:proj_id>/", wefe_simulation, name="wefe_simulation"),
    path("wefesim_simulation/cancel/<int:proj_id>/", wefe_simulation_cancel, name="wefe_simulation_cancel"),
    path("<int:proj_id>/edit/create", wefe_choose_location, name="wefe_scenario_create"),
    path("<int:proj_id>/edit/submit", wefe_choose_location, name="wefe_scenario_submit"),
    path("ajax/generate-survey-link", ajax_generate_survey_link, name="ajax_generate_survey_link"),
    path("ajax/delete-survey", ajax_delete_survey, name="ajax_delete_survey"),
    path("ajax/wefedemand-simulation", request_wefedemand_simulation, name="request_wefedemand_simulation"),
    path(
        "ajax/wefedemand-simulation/<int:proj_id>", request_wefedemand_simulation, name="request_wefedemand_simulation"
    ),
    path("ajax/get-wefedemand-data/<int:proj_id>", get_wefedemand_data, name="get_wefedemand_data"),
    path("ajax/wefesim-simulation", request_wefesim_simulation, name="request_wefesim_simulation"),
    path("ajax/wefesim-simulation/<int:proj_id>", request_wefesim_simulation, name="request_wefesim_simulation"),
    path("ajax/get-wefesim-data/<int:proj_id>", get_wefesim_data, name="get_wefesim_data"),
    # energy-system survey
    path("<int:proj_id>/survey", wefe_system_layout, name="view_survey_questions"),
    path("<int:proj_id>/submit/survey", wefe_system_layout, name="submit_survey"),
    path("<int:proj_id>/view/survey", wefe_system_layout, name="view_survey"),
    path(
        "wefe-simulation/fetch-results/<int:sim_id>",
        fetch_simulation_results,
        name="fetch_wefe_simulation_results",
    ),
    path("documentation", help_page, name="help_page"),
    path("ajax/documentation/selected_step", ajax_help_page, name="ajax_help_page"),
    path("<int:proj_id>/export_pdf/", wefe_export_pdf, name="wefe_export_pdf"),
]
