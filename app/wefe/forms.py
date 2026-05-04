import json

from django import forms
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError

from projects.forms import OpenPlanForm, OpenPlanModelForm
from projects.models import Project, EconomicData, Scenario
from projects.requests import request_exchange_rate
from wefe.models import MOOWeights, SurveyQuestion

from wefe.survey import SURVEY_STRUCTURE, SURVEY_CATEGORIES, TYPE_STRING


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
        exclude = ("country", "user", "viewers", "economic_data", "kobo_survey_id", "kobo_survey_url")

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
        fields = ["population", "duration", "currency", "exchange_rate", "discount"]

    discount = forms.FloatField(
        min_value=0,
        max_value=1,
        initial=0.05,
        widget=forms.NumberInput(attrs={"step": 0.01}),
    )

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


def is_matrix_source(field):

    return "matrix_source" in field.widget.attrs.get("class", "")


class SurveyQuestionForm(forms.Form):
    def __init__(self, *args, **kwargs):
        self.qs_answers = kwargs.pop("qs", [])
        super().__init__(*args, **kwargs)
        for q in SURVEY_STRUCTURE:
            answer = self.qs_answers.get(question__question_id=q["question_id"])
            alv = answer.question.possible_answers
            label = answer.question.question
            if answer.question.description:
                question_icon = f'<span class="icon icon-question" data-bs-toggle="tooltip" title="{answer.question.description}"></span>'
                label += question_icon

            opts = {"label": format_html(f"{label}")}

            # by default the subquestion are not required
            if answer.question.subquestion_to is not None:
                opts["required"] = False

            if alv is not None:
                try:
                    possible_answers = json.loads(alv)
                    list_choices = [(pa, pa.replace("_", " ").capitalize()) for pa in possible_answers]

                    if answer.question.multiple_answers is True:
                        opts["choices"] = list_choices
                        opts["widget"] = forms.CheckboxSelectMultiple(attrs={"class": "checkbox-grid"})
                        self.fields[f"criteria_{answer.question.id}"] = forms.MultipleChoiceField(**opts)
                    else:
                        opts["choices"] = [("", "----------")] + list_choices
                        self.fields[f"criteria_{answer.question.id}"] = forms.ChoiceField(**opts)
                except json.decoder.JSONDecodeError:
                    self.fields[f"criteria_{answer.question.id}"] = forms.FloatField(**opts)
            else:
                if answer.question.answer_type == TYPE_STRING:
                    self.fields[f"criteria_{answer.question.id}"] = forms.CharField(**opts)
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
                    if answer.question.matrix_answers is True:
                        # import pdb;pdb.set_trace()
                        def original_question_number(q_id):
                            answer = q_id
                            for letter in ("a", "b", "c", "d", "e"):
                                if letter in answer:
                                    answer = answer.replace(letter, "")
                            return answer

                        matrix_idxs = original_question_number(answer.question.id).replace(
                            f"{original_question_number(supra_question.id)}.", ""
                        )
                        try:
                            matrix_col_idx, matrix_row_idx = matrix_idxs.split(".")
                        except:
                            print(supra_question.__dict__)
                            print(matrix_idxs)

                        self.fields[f"criteria_{answer.question.id}"].widget.attrs.update(
                            {
                                "class": f"sub_question sub_sub_question matrix_target label_row_{int(matrix_row_idx)+1} matrix_row_{int(matrix_row_idx)+1} matrix_col_{int(matrix_col_idx)+1}"
                            }
                        )
                        # self.fields[f"criteria_{answer.question.id}"].widget.attrs.update({"class": f"sub_question sub_sub_question matrix_target matrix_col_{int(matrix_col_idx)+1}"})
                        supra_question_css = self.fields[f"criteria_{supra_question.id}"].widget.attrs["class"]
                        if "matrix_source" not in supra_question_css:
                            self.fields[f"criteria_{supra_question.id}"].widget.attrs[
                                "class"
                            ] = f"{supra_question_css} matrix_source"
                    else:
                        self.fields[f"criteria_{answer.question.id}"].widget.attrs.update(
                            {"class": "sub_question sub_sub_question"}
                        )
                if is_matrix_source(self.fields[f"criteria_{supra_question.id}"]):
                    # here we know we are within matrix questions
                    self.fields[f"criteria_{supra_question.id}"].widget.attrs.update(
                        {
                            "onchange": f"triggerMatrixSubQuestion(new_value=this,subQuestionMapping={supra_question.subquestion})"
                        }
                    )
                else:

                    self.fields[f"criteria_{supra_question.id}"].widget.attrs.update(
                        {
                            "onchange": f"triggerSubQuestion(new_value=this,subQuestionMapping={supra_question.subquestion})"
                        }
                    )

                # only provide initial value for subquestion if the answer to supraquestion exists and is valid
                supra_answer = self.qs_answers.get(question=supra_question)
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

            # if q.get("display_type") == "multiple_choice_tickbox":
            #     question = q
            #     #print(question)
            #     #import pdb;pdb.set_trace()
            #     q_classes = self.fields[
            #         f"criteria_{answer.question.id}"
            #     ].widget.attrs.get("class")
            #     print(q_classes)
            #     if q_classes is not None:
            #         q_classes = f"{q_classes} multiple_answer"
            #     else:
            #         q_classes = "multiple_answer"
            #     self.fields[
            #         f"criteria_{answer.question.id}"
            #     ].widget.attrs.update(
            #         {
            #             "class": q_classes
            #         }
            #     )

    def clean(self):
        cleaned_data = super().clean()
        errors = {}
        if cleaned_data:
            subquestion_to_erase = []
            for record in cleaned_data:

                question_id = record.replace("criteria_", "")

                question = self.qs_answers.get(question__question_id=question_id).question
                subquestions = question.subquestions
                other_keys = []

                # if the question is a subquestion and the supra question changed, then the subquestion's answer are erased
                if question_id in subquestion_to_erase:
                    cleaned_data[record] = None

                # if the question is a subquestion and the supra question was reinitialized, then the subquestion's answer are erased
                if question.subquestion_to is not None:
                    if f"criteria_{question.subquestion_to.question_id}" not in cleaned_data:
                        cleaned_data[record] = None

                selected_subquestions = []
                if cleaned_data[record] is not None:
                    new_answer = cleaned_data[record]
                    if question.multiple_answers is False:
                        new_answer = [new_answer]

                    if subquestions is not None:
                        other_keys = set(subquestions.keys()) - set(new_answer)

                        for k in new_answer:
                            sq = subquestions.get(k)
                            if isinstance(sq, list):
                                selected_subquestions.extend(sq)
                            elif sq is None:
                                pass
                            else:
                                selected_subquestions.append(sq)
                        selected_subquestions = list(set(selected_subquestions))

                    if cleaned_data[record]:
                        print(record)
                        print(cleaned_data[record])
                        print(type(cleaned_data[record]))
                    else:
                        cleaned_data[record] = None
                    # TODO when a supra answer is given, one need to make sure to cancel the sub answer from subquestions which are not allowed anymore

                else:

                    if subquestions is not None:
                        other_keys = set(subquestions.keys())

                    if question.subquestion_to is None:
                        raise ValidationError("This field cannot be blank")

                for k in other_keys:
                    # TODO if the id is in the combined subquestions[l] for l in cleaned_data[record]
                    # then one does not need to get it out by adding it to the subquestion_to_erase
                    sq = subquestions.get(k)
                    if isinstance(sq, list):
                        for e in sq:
                            if e not in selected_subquestions:
                                subquestion_to_erase.append(e)
                    elif sq is None:
                        pass
                    else:
                        if sq not in selected_subquestions:
                            subquestion_to_erase.append(sq)

                # Perform field validation (check invalid input)
                # Energy Source
                if question_id == "1":
                    ans = cleaned_data[record]
                    if not ans or ans == ["other"]:
                        errors[record] = 'At least one of the provided sources must be selected, excluding "Other".'

                # Wastewater Treatment
                if question_id == "7":
                    ans = cleaned_data[record]
                    if not ans or ans == ["other"]:
                        errors[record] = (
                            'At least one of the provided technologies must be selected, excluding "Other".'
                        )

                # Toilet Type
                if question_id == "7.3":
                    ans = cleaned_data[record]
                    if not ans:
                        errors[record] = "At least one of the provided toilet types must be selected."

                if question_id == "2":
                    # subquestions to validate taken from WATER_SUPPLY_SURVEY_STRUCTURE
                    if cleaned_data[record] == "No":
                        sub_q_to_validate = ["3"]
                    else:
                        sub_q_to_validate = ["3a", "3b"]

                    for q in sub_q_to_validate:
                        subq_record = f"criteria_{q}"
                        ans = cleaned_data[subq_record]
                        if not ans or ans == ["other"]:
                            errors[subq_record] = (
                                'At least one of the provided sources must be selected, excluding "Other".'
                            )
                        else:
                            # validate natural source subquestions (4 and 5) for each suffix
                            natural_sources = {
                                "groundwater well": "_GW",
                                "desalinated seawater": "_DS",
                                "river/creek": "_RC",
                                "lake": "_L",
                            }
                            for source, suffix in natural_sources.items():
                                if source in ans:
                                    full_suffix = suffix + ("a" if q == "3a" else "b" if q == "3b" else "")
                                    # validate question 4
                                    q4_record = f"criteria_4{full_suffix}"
                                    q4_ans = set(cleaned_data.get(q4_record) or [])
                                    primary = {"salinity", "heavy metals", "chemical contamination"}
                                    # secondary = {"fecal contamination", "hardness", "sediments and turbidity", "nitrates and nitrites"}
                                    q4_errors = []
                                    if not q4_ans:
                                        q4_errors.append("At least one water quality issue must be selected.")
                                    else:
                                        # only run specific checks if no primary is selected
                                        if not q4_ans & primary:
                                            # determine which specific message to show based on what secondary is selected
                                            if "nitrates and nitrites" in q4_ans:
                                                q4_errors.append(
                                                    "Nitrates & Nitrites additionally requires Chemical Contamination (Fertilizers) to be selected."
                                                )
                                            if "hardness" in q4_ans:
                                                q4_errors.append(
                                                    "Hardness additionally requires Salinity to be selected."
                                                )
                                            if "fecal contamination" in q4_ans:
                                                q4_errors.append(
                                                    "Fecal Contamination additionally require at least one of Salinity,"
                                                    " Heavy Metals, or Chemical Contamination to be selected."
                                                )
                                            if "sediments and turbidity" in q4_ans:
                                                q4_errors.append(
                                                    "Sediments & Turbidity additionally require at least one of Salinity,"
                                                    " Heavy Metals, or Chemical Contamination to be selected."
                                                )
                                        else:
                                            # primary is present — check specific pairing rules
                                            if "hardness" in q4_ans and "salinity" not in q4_ans:
                                                q4_errors.append(
                                                    "Hardness additionally requires Salinity to be selected."
                                                )
                                            if (
                                                "nitrates and nitrites" in q4_ans
                                                and "chemical contamination" not in q4_ans
                                            ):
                                                q4_errors.append(
                                                    "Nitrates & Nitrites additionally requires Chemical Contamination (Fertilizers) to be selected."
                                                )
                                    if q4_errors:
                                        errors[q4_record] = " ".join(q4_errors)

                                    # validate 4.1 — salinity value
                                    q4_1_record = f"criteria_4{full_suffix}.1"
                                    q4_1_ans = cleaned_data.get(q4_1_record)
                                    if "salinity" in q4_ans and (q4_1_ans is None or q4_1_ans <= 0):
                                        errors[q4_1_record] = "Salinity value must be greater than 0."

                                    # validate 4.2 — heavy metals
                                    q4_2_record = f"criteria_4{full_suffix}.2"
                                    if "heavy metals" in q4_ans and not cleaned_data.get(q4_2_record):
                                        errors[q4_2_record] = "At least one heavy metal must be selected."

                                    # validate 4.3 — chemical contaminants
                                    q4_3_record = f"criteria_4{full_suffix}.3"
                                    if "chemical contamination" in q4_ans and not cleaned_data.get(q4_3_record):
                                        errors[q4_3_record] = "At least one chemical contaminant must be selected."

                                    # validate question 5
                                    q5_record = f"criteria_5{full_suffix}"
                                    q5_ans = cleaned_data.get(q5_record)
                                    if not q5_ans or q5_ans == ["other"]:
                                        errors[q5_record] = (
                                            'At least one treatment technology or "no" must be selected, excluding "Other".'
                                        )

            for record, msg in errors.items():
                self.add_error(record, msg)

        else:
            raise ValidationError("This form cannot be blank")
        return cleaned_data


class MOOForm(forms.ModelForm):
    # multi-objective optimization setup
    class Meta:
        model = MOOWeights
        exclude = ["scenario"]

    total_cost = forms.FloatField(
        min_value=0, max_value=1, initial=1, widget=forms.NumberInput(attrs={"step": 0.1, "default": 1})
    )
    co2_emissions = forms.FloatField(
        min_value=0, max_value=1, initial=0, widget=forms.NumberInput(attrs={"step": 0.1, "default": 0})
    )
    land_requirements = forms.FloatField(
        min_value=0, max_value=1, initial=0, widget=forms.NumberInput(attrs={"step": 0.1, "default": 0})
    )
    water_footprint = forms.FloatField(
        min_value=0, max_value=1, initial=0, widget=forms.NumberInput(attrs={"step": 0.1, "default": 0})
    )

    def clean(self):
        # check that weights add up to 1
        cleaned_data = super().clean()
        cost = cleaned_data.get("total_cost")
        co2 = cleaned_data.get("co2_emissions")
        land = cleaned_data.get("land_requirements")
        water = cleaned_data.get("water_footprint")
        if cost is not None and co2 is not None and land is not None and water is not None:
            if round(cost + co2 + land + water, 4) != 1:
                raise ValidationError("Weights must add up to 1")
