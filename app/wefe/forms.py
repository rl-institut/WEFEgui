import json

from django import forms
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError

from projects.forms import OpenPlanForm, OpenPlanModelForm, ProjectCreateForm
from projects.models import Project, EconomicData, Scenario
from projects.requests import request_exchange_rate
from wefe.models import SurveyQuestion


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
        initial_currency = "USD"
        self.fields["currency"].initial = initial_currency

        if instance is None:
            self.fields["exchange_rate"].initial = request_exchange_rate(initial_currency)


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


class SurveyQuestionForm(forms.Form):
    def __init__(self, *args, **kwargs):
        qs = kwargs.pop("qs", [])
        super().__init__(*args, **kwargs)
        for answer in qs:
            alv = answer.question.possible_answers
            opts = {"label": f"{answer.question.question_id}: {answer.question.question}"}

            # by default the subquestion are not required
            if answer.question.subquestion_to is not None:
                opts["required"] = False

            if alv is not None:
                try:
                    possible_answers = json.loads(alv)
                    list_choices = [(pa, pa.replace("_", " ").capitalize()) for pa in possible_answers]

                    if answer.question.multiple_answers is True:
                        opts["choices"] = list_choices
                        opts["widget"] = forms.CheckboxSelectMultiple
                        self.fields[f"criteria_{answer.question.id}"] = forms.MultipleChoiceField(**opts)
                    else:
                        opts["choices"] = [("", "----------")] + list_choices
                        self.fields[f"criteria_{answer.question.id}"] = forms.ChoiceField(**opts)
                except json.decoder.JSONDecodeError:
                    self.fields[f"criteria_{answer.question.id}"] = forms.FloatField(**opts)
            else:
                self.fields[f"criteria_{answer.question.id}"] = forms.FloatField(**opts)

            # treat sub question differently:
            # - links to onchange of supra question
            # - hide the sub question if the supra question's answer is not "Yes"
            if answer.question.subquestion_to is not None:
                supra_question = SurveyQuestion.objects.get(question_id=answer.question.subquestion_to.question_id)

                # subquestion class
                self.fields[f"criteria_{answer.question.id}"].widget.attrs.update({"class": "sub_question"})

                # subsubquestion class
                if supra_question.subquestion_to is not None:
                    self.fields[f"criteria_{answer.question.id}"].widget.attrs.update(
                        {"class": "sub_question sub_sub_question"}
                    )

                if f"criteria_{supra_question.id}" in self.fields:
                    self.fields[f"criteria_{supra_question.id}"].widget.attrs.update(
                        {
                            "onchange": f"triggerSubQuestion(new_value=this,subQuestionMapping={supra_question.subquestion})"
                        }
                    )

                # only provide initial value for subquestion if the answer to supraquestion exists and is valid
                supra_answer = qs.get(question=supra_question)
                if supra_answer.value is not None:
                    if answer.value:
                        if answer.question.multiple_answers is True:
                            self.fields[f"criteria_{answer.question.id}"].initial = json.loads(
                                answer.value.replace("'", '"')
                            )
                        else:
                            self.fields[f"criteria_{answer.question.id}"].initial = answer.value

            else:
                if answer.value:
                    if answer.question.multiple_answers is True:
                        self.fields[f"criteria_{answer.question.id}"].initial = json.loads(
                            answer.value.replace("'", '"')
                        )
                    else:
                        self.fields[f"criteria_{answer.question.id}"].initial = answer.value

    def clean(self):
        cleaned_data = super().clean()

        if cleaned_data:
            for record in cleaned_data:
                if cleaned_data[record] is not None:

                    if cleaned_data[record]:
                        print(record)
                        print(cleaned_data[record])
                        print(type(cleaned_data[record]))
                    else:
                        cleaned_data[record] = None
                    # TODO need to look at the expected type of the question's answer
                else:
                    question = SurveyQuestion.objects.get(question_id=record.replace("criteria_", ""))
                    if question.subquestion_to is None:
                        raise ValidationError("This field cannot be blank")
        else:
            raise ValidationError("This form cannot be blank")
        return cleaned_data
