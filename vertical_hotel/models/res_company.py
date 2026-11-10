# Copyright (C) 2024-TODAY Serpent Consulting Services Pvt. Ltd. (<http://www.serpentcs.com>).
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    additional_hours = fields.Integer(
        help="Provide the min hours value for \
                                      check in, checkout days, whatever the \
                                      hours will be provided here based on \
                                      that extra days will be calculated.",
    )

    def _get_hotel_stay_nights(self, checkin, checkout):
        """Number of nights to bill for a stay from ``checkin`` to ``checkout``.

        Every whole 24 hours is a night. What is left over counts as one
        more night when the company has no Additional Hours, or when it
        lasts at least that many hours; it is never counted twice. A stay
        is at least one night.

        With 2 additional hours, 14:00 to 11:00 the next day (21 hours) is
        1 night, and so is a stay until 14:30 the next day (the 30 minutes
        are below the 2 hours), while a stay until 17:00 the next day is 2.
        """
        if not checkin or not checkout or checkout <= checkin:
            return 0
        stay = checkout - checkin
        nights = stay.days
        additional_hours = self.additional_hours
        if stay.seconds and (
            not additional_hours or stay.seconds >= additional_hours * 3600
        ):
            nights += 1
        return max(nights, 1)
