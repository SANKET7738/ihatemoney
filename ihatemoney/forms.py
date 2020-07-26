from datetime import datetime
from re import match

import email_validator
from flask import request
from flask_babel import lazy_gettext as _
from flask_wtf.file import FileAllowed, FileField, FileRequired
from flask_wtf.form import FlaskForm
from jinja2 import Markup
from werkzeug.security import check_password_hash, generate_password_hash
from wtforms.fields.core import Label, SelectField, SelectMultipleField
from wtforms.fields.html5 import DateField, DecimalField, URLField
from wtforms.fields.simple import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import (
    DataRequired,
    Email,
    EqualTo,
    NumberRange,
    Optional,
    ValidationError,
)

from ihatemoney.currency_convertor import CurrencyConverter
from ihatemoney.models import LoggingMode, Person, Project
from ihatemoney.utils import (
    eval_arithmetic_expression,
    render_localized_currency,
    slugify,
)


def strip_filter(string):
    try:
        return string.strip()
    except Exception:
        return string


def get_billform_for(project, set_default=True, **kwargs):
    """Return an instance of BillForm configured for a particular project.

    :set_default: if set to True, on GET methods (usually when we want to
                  display the default form, it will call set_default on it.

    """
    form = BillForm(**kwargs)
    if form.original_currency.data == "None":
        form.original_currency.data = project.default_currency

    show_no_currency = form.original_currency.data == CurrencyConverter.no_currency

    form.original_currency.choices = [
        (currency_name, render_localized_currency(currency_name, detailed=False))
        for currency_name in form.currency_helper.get_currencies(
            with_no_currency=show_no_currency
        )
    ]

    active_members = [(m.id, m.name) for m in project.active_members]

    form.payed_for.choices = form.payer.choices = active_members
    form.payed_for.default = [m.id for m in project.active_members]

    if set_default and request.method == "GET":
        form.set_default()
    return form


class CommaDecimalField(DecimalField):

    """A class to deal with comma in Decimal Field"""

    def process_formdata(self, value):
        if value:
            value[0] = str(value[0]).replace(",", ".")
        return super(CommaDecimalField, self).process_formdata(value)


class CalculatorStringField(StringField):
    """
    A class to deal with math ops (+, -, *, /)
    in StringField
    """

    def process_formdata(self, valuelist):
        if valuelist:
            message = _(
                "Not a valid amount or expression. "
                "Only numbers and + - * / operators "
                "are accepted."
            )
            value = str(valuelist[0]).replace(",", ".")

            # avoid exponents to prevent expensive calculations i.e 2**9999999999**9999999
            if not match(r"^[ 0-9\.\+\-\*/\(\)]{0,200}$", value) or "**" in value:
                raise ValueError(Markup(message))

            valuelist[0] = str(eval_arithmetic_expression(value))

        return super(CalculatorStringField, self).process_formdata(valuelist)


class EditProjectForm(FlaskForm):
    name = StringField(_("Project name"), validators=[DataRequired()])
    password = StringField(_("Private code"), validators=[DataRequired()])
    contact_email = StringField(_("Email"), validators=[DataRequired(), Email()])
    project_history = BooleanField(_("Enable project history"))
    ip_recording = BooleanField(_("Use IP tracking for project history"))
    currency_helper = CurrencyConverter()
    default_currency = SelectField(_("Default Currency"), validators=[DataRequired()],)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_currency.choices = [
            (
                currency_name,
                render_localized_currency(currency_name, detailed=True)
                + (
                    " − " + _("⚠ All bill currencies will be removed")
                    if currency_name == self.currency_helper.no_currency
                    else ""
                ),
            )
            for currency_name in self.currency_helper.get_currencies()
        ]

    @property
    def logging_preference(self):
        """Get the LoggingMode object corresponding to current form data."""
        if not self.project_history.data:
            return LoggingMode.DISABLED
        else:
            if self.ip_recording.data:
                return LoggingMode.RECORD_IP
            else:
                return LoggingMode.ENABLED

    def save(self):
        """Create a new project with the information given by this form.

        Returns the created instance
        """
        project = Project(
            name=self.name.data,
            id=self.id.data,
            password=generate_password_hash(self.password.data),
            contact_email=self.contact_email.data,
            logging_preference=self.logging_preference,
            default_currency=self.default_currency.data,
        )
        return project

    def update(self, project):
        """Update the project with the information from the form"""
        project.name = self.name.data

        # Only update password if changed to prevent spurious log entries
        if not check_password_hash(project.password, self.password.data):
            project.password = generate_password_hash(self.password.data)

        project.contact_email = self.contact_email.data
        project.logging_preference = self.logging_preference
        project.switch_currency(self.default_currency.data)

        return project


class UploadForm(FlaskForm):
    file = FileField(
        "JSON",
        validators=[FileRequired(), FileAllowed(["json", "JSON"], "JSON only!")],
        description=_("Import previously exported JSON file"),
    )
    submit = SubmitField(_("Import"))


class ProjectForm(EditProjectForm):
    id = StringField(_("Project identifier"), validators=[DataRequired()])
    password = PasswordField(_("Private code"), validators=[DataRequired()])
    submit = SubmitField(_("Create the project"))

    def save(self):
        # WTForms Boolean Fields don't insert the default value when the
        # request doesn't include any value the way that other fields do,
        # so we'll manually do it here
        self.project_history.data = LoggingMode.default() != LoggingMode.DISABLED
        self.ip_recording.data = LoggingMode.default() == LoggingMode.RECORD_IP
        return super().save()

    def validate_id(form, field):
        form.id.data = slugify(field.data)
        if (form.id.data == "dashboard") or Project.query.get(form.id.data):
            message = _(
                'A project with this identifier ("%(project)s") already exists. '
                "Please choose a new identifier",
                project=form.id.data,
            )
            raise ValidationError(Markup(message))


class AuthenticationForm(FlaskForm):
    id = StringField(_("Project identifier"), validators=[DataRequired()])
    password = PasswordField(_("Private code"), validators=[DataRequired()])
    submit = SubmitField(_("Get in"))


class AdminAuthenticationForm(FlaskForm):
    admin_password = PasswordField(_("Admin password"), validators=[DataRequired()])
    submit = SubmitField(_("Get in"))


class PasswordReminder(FlaskForm):
    id = StringField(_("Project identifier"), validators=[DataRequired()])
    submit = SubmitField(_("Send me the code by email"))

    def validate_id(form, field):
        if not Project.query.get(field.data):
            raise ValidationError(_("This project does not exists"))


class ResetPasswordForm(FlaskForm):
    password_validators = [
        DataRequired(),
        EqualTo("password_confirmation", message=_("Password mismatch")),
    ]
    password = PasswordField(_("Password"), validators=password_validators)
    password_confirmation = PasswordField(
        _("Password confirmation"), validators=[DataRequired()]
    )
    submit = SubmitField(_("Reset password"))


class BillForm(FlaskForm):
    date = DateField(_("Date"), validators=[DataRequired()], default=datetime.now)
    what = StringField(_("What?"), validators=[DataRequired()])
    payer = SelectField(_("Payer"), validators=[DataRequired()], coerce=int)
    amount = CalculatorStringField(_("Amount paid"), validators=[DataRequired()])
    currency_helper = CurrencyConverter()
    original_currency = SelectField(_("Currency"), validators=[DataRequired()],)
    external_link = URLField(
        _("External link"),
        validators=[Optional()],
        description=_("A link to an external document, related to this bill"),
    )
    payed_for = SelectMultipleField(
        _("For whom?"), validators=[DataRequired()], coerce=int
    )
    submit = SubmitField(_("Submit"))
    submit2 = SubmitField(_("Submit and add a new one"))

    def save(self, bill, project):
        bill.payer_id = self.payer.data
        bill.amount = self.amount.data
        bill.what = self.what.data
        bill.external_link = self.external_link.data
        bill.date = self.date.data
        bill.owers = [Person.query.get(ower, project) for ower in self.payed_for.data]
        bill.original_currency = self.original_currency.data
        bill.converted_amount = self.currency_helper.exchange_currency(
            bill.amount, bill.original_currency, project.default_currency
        )
        return bill

    def fake_form(self, bill, project):
        bill.payer_id = self.payer
        bill.amount = self.amount
        bill.what = self.what
        bill.external_link = ""
        bill.date = self.date
        bill.owers = [Person.query.get(ower, project) for ower in self.payed_for]
        bill.original_currency = CurrencyConverter.no_currency
        bill.converted_amount = self.currency_helper.exchange_currency(
            bill.amount, bill.original_currency, project.default_currency
        )

        return bill

    def fill(self, bill, project):
        self.payer.data = bill.payer_id
        self.amount.data = bill.amount
        self.what.data = bill.what
        self.external_link.data = bill.external_link
        self.original_currency.data = bill.original_currency
        self.date.data = bill.date
        self.payed_for.data = [int(ower.id) for ower in bill.owers]

        self.original_currency.label = Label("original_currency", _("Currency"))
        self.original_currency.description = _(
            "Project default: %(currency)s",
            currency=render_localized_currency(
                project.default_currency, detailed=False
            ),
        )

    def set_default(self):
        self.payed_for.data = self.payed_for.default

    def validate_amount(self, field):
        if field.data == 0:
            raise ValidationError(_("Bills can't be null"))


class MemberForm(FlaskForm):
    name = StringField(_("Name"), validators=[DataRequired()], filters=[strip_filter])

    weight_validators = [NumberRange(min=0.1, message=_("Weights should be positive"))]
    weight = CommaDecimalField(_("Weight"), default=1, validators=weight_validators)
    submit = SubmitField(_("Add"))

    def __init__(self, project, edit=False, *args, **kwargs):
        super(MemberForm, self).__init__(*args, **kwargs)
        self.project = project
        self.edit = edit

    def validate_name(form, field):
        if field.data == form.name.default:
            raise ValidationError(_("User name incorrect"))
        if (
            not form.edit
            and Person.query.filter(
                Person.name == field.data,
                Person.project == form.project,
                Person.activated == True,
            ).all()
        ):  # NOQA
            raise ValidationError(_("This project already have this member"))

    def save(self, project, person):
        # if the user is already bound to the project, just reactivate him
        person.name = self.name.data
        person.project = project
        person.weight = self.weight.data

        return person

    def fill(self, member):
        self.name.data = member.name
        self.weight.data = member.weight


class InviteForm(FlaskForm):
    emails = StringField(_("People to notify"), render_kw={"class": "tag"})
    submit = SubmitField(_("Send invites"))

    def validate_emails(form, field):
        for email in [email.strip() for email in form.emails.data.split(",")]:
            try:
                email_validator.validate_email(email)
            except email_validator.EmailNotValidError:
                raise ValidationError(
                    _("The email %(email)s is not valid", email=email)
                )
