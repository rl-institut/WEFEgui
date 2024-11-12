from django import forms
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError

from projects.forms import OpenPlanForm, OpenPlanModelForm, ProjectCreateForm
from projects.models import Project, EconomicData, Scenario
from projects.requests import request_exchange_rate


def validate_not_zero(value):
    if value == 0:
        raise ValidationError(_("This field cannot be equal to 0"))


class ProjectForm(OpenPlanModelForm):
    start_date = forms.DateField(
        label=_("Simulation start"),
        initial=f"{timezone.now().year}-01-01",
        widget=forms.DateInput(
            format="%Y-%m-%d",
            attrs={
                "class": "TestDateClass",
                "placeholder": "Select a start date",
                "type": "date",
            },
        ),
    )

    class Meta:
        model = Project
        exclude = ("country", "user", "viewers", "economic_data")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["description"].required = False

    def save(self, *args, **kwargs):
        user = kwargs.pop("user")
        kwargs["commit"] = False
        pr = super().save(*args, **kwargs)

        # The project does not exist yet so we created it as well as a scenario
        if pr.id is None:
            pr.user = user
            pr.country = "NIGERIA"
            pr.save()
            Scenario.objects.create(
                name=f'{self.cleaned_data["name"]}_scenario',
                start_date=self.cleaned_data["start_date"],
                time_step=60,
                evaluated_period=365,  # TODO this depends on the year
                project=pr,
            )
        # The project does exist and we update simply its values
        else:
            scenario = Scenario.objects.filter(project=pr)
            scenario.update(start_date=self.cleaned_data["start_date"])
            pr.save()

        return pr


class EconomicProjectForm(OpenPlanModelForm):
    class Meta:
        model = EconomicData
        fields = ["duration", "currency", "exchange_rate"]

    def __init__(self, *args, **kwargs):
        instance = kwargs.get("instance", None)
        super().__init__(*args, **kwargs)
        self.fields["currency"].initial = "NGN"

        if instance is None:
            self.fields["exchange_rate"].initial = request_exchange_rate("NGN")


class EconomicDataForm(OpenPlanModelForm):
    capex_fix = forms.FloatField(
        label=_("Fix project costs"),
        help_text=_("Expected additional costs, e.g. for project planning, land purchase etc."),
        validators=[MinValueValidator(0.0)],
    )

    class Meta:
        model = EconomicData
        exclude = ("currency", "duration", "exchange_rate")

    def __init__(self, *args, **kwargs):
        instance = kwargs.get("instance", None)
        initial = kwargs.get("initial", {})
        if instance is not None:
            for field in ["discount", "tax"]:
                initial_value = getattr(instance, field)
                if initial_value is not None:
                    initial[field] = initial_value * 100

        kwargs["initial"] = initial

        super().__init__(*args, **kwargs)
        self.fields["discount"].validators.append(validate_not_zero)

    # def save(self, *args, **kwargs):
    #     ed = super().save(*args, **kwargs)
    #     scenario = Scenario.objects.filter(project__economic_data=ed)
    #     scenario.update(capex_fix=self.cleaned_data["capex_fix"])

    def clean(self):
        """Convert the percentage values into values ranging from 0 to 1 (for further calculations)"""
        super().clean()
        for field, value in self.cleaned_data.items():
            if field in ["discount", "tax"]:
                self.cleaned_data[field] = value / 100

        return self.cleaned_data
