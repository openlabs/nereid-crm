# -*- coding: utf-8 -*-
"""
    opportunnity

    Mini CRM based on Nereid and Sale Opprotunity

    :copyright: (c) 2012-2013 by Openlabs Technologies & Consulting (P) Limited
    :license: GPLv3, see LICENSE for more details.
"""
from decimal import Decimal
import logging
from wtforms import (Form, IntegerField, TextField, SelectField, TextAreaField,
    validators)
from wtfrecaptcha.fields import RecaptchaField

from nereid import (request, abort, render_template, login_required, url_for,
    redirect, flash, jsonify, permissions_required, render_email)
from nereid.contrib.pagination import Pagination
from trytond.model import ModelView, ModelSQL, ModelSingleton, Workflow, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Eval
from trytond.config import CONFIG
from trytond.tools import get_smtp_server


__all__ = [
    'Configuration', 'NereidReview', 'CompanySalesTeam', 'Company',
    'SaleOpportunity', 'NereidUser',
]
__metaclass__ = PoolMeta

geoip = None
try:
    from pygeoip import GeoIP
except ImportError:
    logging.error("pygeoip is not installed")
else:
    try:
        # Usual location in Ubuntu
        geoip = GeoIP('/usr/share/GeoIP/GeoIP.dat')
    except IOError:
        try:
            # this is where brew installs it
            geoip = GeoIP(
                '/usr/local/Cellar/geoip/1.4.8/share/GeoIP/GeoIP.dat'
            )
        except IOError:
            pass


class NereidUser:
    """
    Add employee
    """
    __name__ = "nereid.user"

    #: Allow the nereid user to be connected to an internal employee. This
    #: indicates that the user is an employee and not a regular participant
    employee = fields.Many2One(
        'company.employee', 'Employee', select=True,
    )


class Configuration:
    "Sale Opportunity configuration"
    __name__ = 'sale.configuration'

    website_employee = fields.Property(
        fields.Many2One('company.employee', 'Website Employee')
    )


class ContactUsForm(Form):
    "Simple Contact Us form"
    name = TextField('Name', [validators.Required(),])
    company = TextField('Company')
    country = SelectField('Country', [validators.Required(),], coerce=int)
    email = TextField('e-mail', [validators.Required(), validators.Email()])
    if 're_captcha_public' in CONFIG.options:
        captcha = RecaptchaField(
            public_key=CONFIG.options['re_captcha_public'],
            private_key=CONFIG.options['re_captcha_private'], secure=True)
    website = TextField('Website')
    phone = TextField('Phone')
    comment = TextAreaField('Comment', [validators.Required(),])


class SaleOpportunity:
    "Sale Opportunity"
    __name__ = "sale.opportunity"

    ip_address = fields.Char('IP Address')
    reviews = fields.One2Many(
        'nereid.review',
        'lead', 'Reviews'
    )
    detected_country = fields.Char('Detected Country')

    contactus_form = ContactUsForm

    @classmethod
    def new_opportunity(cls):
        """
        Web handler to create a new sale opportunity
        """
        Country = Pool().get('country.country')

        if 're_captcha_public' in CONFIG.options:
            contact_form = cls.contactus_form(
                request.form,
                captcha={'ip_address': request.remote_addr}
            )
        else:
            contact_form = cls.contactus_form(request.form)

        countries = Country.search([])

        contact_form.country.choices = [
            (c.id, c.name) for c in countries
        ]

        if request.method == 'POST' and contact_form.validate():
            Address = Pool().get('party.address')
            ContactMech = Pool().get('party.contact_mechanism')
            Party = Pool().get('party.party')
            Config = Pool().get('sale.configuration')
            Company = Pool().get('company.company')
            Country = Pool().get('country.country')

            config = Config(1)
            contact_data = contact_form.data
            # Create Party
            company = request.nereid_website.company.id

            if not contact_data.get('country', None) and geoip:
                detected_country = geoip.country_name_by_addr(
                    request.remote_addr
                )
            else:
                detected_country = ''

            party = Party.create({
                'name': contact_data.get('company') or \
                    contact_data['name'],
                'addresses': [
                    ('create', {
                        'name': contact_data['name'],
                        'country': contact_data['country'],
                        })],
                })

            if contact_data.get('website'):
                # Create website as contact mech
                contact_mech = ContactMech.create({
                        'type': 'website',
                        'party': party.id,
                        'website': contact_data['website'],
                    })

            if contact_data.get('phone'):
                # Create phone as contact mech and assign as phone
                contact_mech = ContactMech.create({
                        'type': 'phone',
                        'party': party.id,
                        'other_value': contact_data['phone'],
                    })
                Address.write(
                    [party.addresses[0]], {'phone': contact_data['phone']}
                )

            # Create email as contact mech and assign as email
            contact_mech = ContactMech.create({
                    'type': 'email',
                    'party': party.id,
                    'email': contact_data['email'],
                })
            Address.write(
                [party.addresses[0]], {'email': contact_data['email']}
            )

            # Create sale opportunity
            if request.nereid_user.employee:
                employee = request.nereid_user.employee.id
                description = 'Created by %s' % \
                    request.nereid_user.display_name
            else:
                employee = config.website_employee.id
                description =  'Created from website'
            employee = request.nereid_user.employee.id \
                if request.nereid_user.employee else config.website_employee.id
            lead = cls.create({
                'party': party.id,
                'company': company,
                'employee': employee,
                'address': party.addresses[0].id,
                'description': description,
                'comment': contact_data['comment'],
                'ip_address': request.remote_addr,
                'detected_country': detected_country,
            })
            lead.send_notification_mail()

            return redirect(request.args.get('next',
                url_for('sale.opportunity.admin_lead', active_id=lead.id)))
        return render_template('crm/sale_form.jinja', form=contact_form)

    def send_notification_mail(self):
        """
        Send a notification mail to sales department whenever there is query
        for new lead.

        :param lead_id: ID of lead.
        """
        # Prepare the content for email.
        subject = "[Openlabs CRM] New lead created by %s" % (self.party.name)

        receivers = [member.email for member in self.company.sales_team
                     if member.email]
        if not receivers:
            return

        message = render_email(
            from_email=CONFIG['smtp_from'],
            to=', '.join(receivers),
            subject=subject,
            text_template='crm/emails/notification_text.jinja',
            lead=self
        )

        # Send mail.
        server = get_smtp_server()
        server.sendmail(
            CONFIG['smtp_from'], receivers, message.as_string()
        )
        server.quit()

    @classmethod
    def new_opportunity_thanks(cls):
        "A thanks template rendered"
        return render_template('crm/thanks.jinja')

    @login_required
    @permissions_required(['sales.admin'])
    def revenue_opportunity(self):
        """
        Set the Conversion Probability and estimated revenue amount
        """
        NereidUser = Pool().get('nereid.user')

        nereid_user = NereidUser.search(
            [('employee', '=', self.employee.id)], limit=1
        )
        if nereid_user:
            employee = nereid_user[0]
        else:
            employee = None

        if request.method == 'POST':
            self.write([self], {
                'probability': request.form['probability'],
                'amount': Decimal(request.form.get('amount'))
            })
            flash('Lead has been updated.')
            return redirect(url_for(
                'sale.opportunity.admin_lead', active_id=self.id) + "#tab-revenue"
            )
        return render_template(
            'crm/admin-lead.jinja', lead=self, employee=employee,
        )

    @classmethod
    @login_required
    @permissions_required(['sales.admin'])
    def sales_home(cls):
        """
        Shows a home page for the sale opportunities
        """
        Country = Pool().get('country.country')

        countries = Country.search([])

        counter = {}
        for state in ('lead', 'opportunity', 'converted', 'cancelled', 'lost'):
            counter[state] = cls.search([('state', '=', state)], count=True)
        return render_template(
            'crm/home.jinja', counter=counter, countries=countries
        )

    @login_required
    @permissions_required(['sales.admin'])
    def assign_lead(self):
        "Change the employee on lead"
        NereidUser = Pool().get('nereid.user')

        new_assignee = NereidUser(int(request.form['user']))
        if self.employee.id == new_assignee.employee.id:
            flash("Lead already assigned to %s" % new_assignee.name)
            return redirect(request.referrer)

        self.write([self], {
            'employee': new_assignee.employee.id
        })

        flash("Lead assigned to %s" % new_assignee.name)
        return redirect(request.referrer)

    @classmethod
    @login_required
    @permissions_required(['sales.admin'])
    def all_leads(cls, page=1):
        """
        All leads captured
        """
        Country = Pool().get('country.country')

        countries = Country.search([])
        filter_domain = []

        company = request.args.get('company', None)
        if company:
            filter_domain.append(('party.name', 'ilike', '%%%s%%' % company))

        name = request.args.get('name', None)
        if name:
            filter_domain.append(
                ('address.name', 'ilike', '%%%s%%' % name)
            )

        email = request.args.get('email', None)
        if email:
            filter_domain.append(
                ('address.email', 'ilike', '%%%s%%' % email)
            )

        state = request.args.get('state', None)
        if state:
            filter_domain.append(
                ('state', '=', '%s' % state)
            )

        leads = Pagination(cls, filter_domain, page, 10)
        return render_template(
            'crm/leads.jinja', leads=leads, countries=countries
        )

    @login_required
    @permissions_required(['sales.admin'])
    def admin_lead(self):
        """
        Lead
        """
        NereidUser = Pool().get('nereid.user')
        Country = Pool().get('country.country')

        countries = Country.search([])
        nereid_users = NereidUser.search(
            [('employee', '=', self.employee.id)], limit=1
        )
        if nereid_users:
            employee = nereid_users[0]
        else:
            employee = None
        return render_template(
            'crm/admin-lead.jinja', lead=self, employee=employee,
            countries=countries
        )

    @classmethod
    @login_required
    @permissions_required(['sales.admin'])
    def add_comment(cls):
        """
        Add a comment for the lead
        """
        Review = Pool().get('nereid.review')
        lead_id = request.form.get('lead', type=int)
        lead = cls(lead_id)

        Review.create({
            'lead': lead.id,
            'title': request.form.get('title'),
            'comment': request.form.get('comment'),
            'nereid_user': request.nereid_user.id,
            'party': lead.party.id,
        })
        if request.is_xhr:
            return jsonify({
                'success': True,
                'message': 'The comment has been added.'
            })
        return redirect(request.referrer + '#tab-comment')

    @login_required
    @permissions_required(['sales.admin'])
    def mark_opportunity(self):
        """
        Convert the lead to opportunity
        """
        self.opportunity([self])
        if request.is_xhr:
            return jsonify({
                'success': True,
                'message': 'Good Work! This lead is an opportunity now.'
            })
        return redirect(request.referrer)

    @login_required
    @permissions_required(['sales.admin'])
    def mark_lost(self):
        """
        Convert the lead to lost
        """
        self.lost([self])
        if request.is_xhr:
            return jsonify({
                'success': True,
                'message': 'The lead is marked as lost.'
            })
        return redirect(request.referrer)

    @login_required
    @permissions_required(['sales.admin'])
    def mark_lead(self):
        """
        Convert the opportunity to lead
        """
        self.lead([self])
        if request.is_xhr:
            return jsonify({
                'success': True,
                'message': 'The lead is marked back to open.'
            })
        return redirect(request.referrer)

    @login_required
    @permissions_required(['sales.admin'])
    def mark_converted(self):
        """
        Convert the opportunity
        """
        self.convert([self])
        if request.is_xhr:
            return jsonify({
                'success': True,
                'message': 'Awesome! The Opportunity is converted.'
            })
        return redirect(request.referrer)

    @login_required
    @permissions_required(['sales.admin'])
    def mark_cancelled(self):
        """
        Convert the lead as cancelled
        """
        self.cancel([self])
        if request.is_xhr:
            return jsonify({
                'success': True,
                'message': 'The lead is cancelled.'
            })
        return redirect(request.referrer)



class Company:
    "Company"
    __name__ = 'company.company'

    sales_team = fields.Many2Many(
        'company.company-nereid.user-sales',
        'company', 'nereid_user', 'Sales Team'
    )


class CompanySalesTeam(ModelSQL):
    "Sales Team"
    __name__ = 'company.company-nereid.user-sales'
    _table = 'company_nereid_sales_team_rel'

    company = fields.Many2One('company.company', 'Company', ondelete='CASCADE',
        required=True, select=True
    )
    nereid_user = fields.Many2One('nereid.user', 'Nereid User',
        ondelete='CASCADE', required=True, select=True,
    )


class NereidReview:
    """
    Nereid Review
    """
    __name__ = "nereid.review"

    lead = fields.Many2One(
        'sale.opportunity', 'Sale Opportunity Lead'
    )
