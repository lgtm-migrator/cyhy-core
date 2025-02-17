__all__ = ["VulnTicketManager", "IPPortTicketManager", "IPTicketManager"]

from collections import defaultdict
from dateutil import relativedelta, tz

from cyhy.core.common import TICKET_EVENT, UNKNOWN_OWNER
from cyhy.db.queries import close_tickets_pl, clear_latest_vulns_pl
from cyhy.db import database
from cyhy.util import util

from netaddr import IPSet

MAX_PORTS_COUNT = 65535


class VulnTicketManager(object):
    """Handles the opening and closing of tickets for a vulnerability scan"""

    def __init__(self, db, source, reopen_days=90, manual_scan=False):
        self.__closing_time = None
        self.__db = db
        self.__ips = IPSet()
        self.__manual_scan = manual_scan
        self.__ports = set()
        self.__reopen_delta = relativedelta.relativedelta(days=-reopen_days)
        self.__seen_ticket_ids = set()
        self.__source = source
        self.__source_ids = set()

    @property
    def ips(self):
        return self.__ips

    @ips.setter
    def ips(self, ips):
        self.__ips = IPSet(ips)

    @property
    def ports(self):
        return self.__ports

    @ports.setter
    def ports(self, ports):
        self.__ports = set(ports)
        # General vulns will be on port 0,
        # but nmap will never send 0 as open.
        # we'll add it here so it'll always be considered
        self.__ports.add(0)

    @property
    def source_ids(self):
        return self.__source_ids

    @source_ids.setter
    def source_ids(self, source_ids):
        self.__source_ids = set(source_ids)

    def __mark_seen(self, vuln):
        self.__seen_ticket_ids.add(vuln["_id"])

    def __calculate_delta(self, d1, d2):
        """d1 and d2 are dictionaries.  Returns a list of changes."""
        delta = []
        all_keys = set(d1.keys() + d2.keys())
        for k in all_keys:
            v1 = d1.get(k)
            v2 = d2.get(k)
            if v1 != v2:
                delta.append({"key": k, "from": v1, "to": v2})
        return delta

    def __check_false_positive_expiration(self, ticket, time):
        # if false_positive expiration date has been reached,
        # flip false_positive flag and add CHANGED event
        if ticket["false_positive"] is True:
            fp_effective_date, fp_expiration_date = ticket.false_positive_dates
            if fp_expiration_date < time:
                ticket["false_positive"] = False
                event = {
                    "action": TICKET_EVENT.CHANGED,
                    "delta": [{"from": True, "to": False, "key": "false_positive"}],
                    "reason": "False positive expired",
                    "reference": None,
                    "time": time,
                }
                if self.__manual_scan:
                    event["manual"] = True
                ticket["events"].append(event)

    def __generate_ticket_details(self, vuln, ticket, check_for_changes=True):
        """Generate the contents of the ticket's details field using NVD data.

        If check_for_changes is True, it will detect changes in the details,
        and generate a CHANGED event.  If a delta is generated, it will be
        returned.  If no delta is generated, an empty list is returned."""

        new_details = {
            "cve": vuln.get("cve"),
            "cvss_base_score": vuln.get("cvss3_base_score", vuln["cvss_base_score"]),
            "cvss_version": "3" if "cvss3_base_score" in vuln else "2",
            "kev": False,
            "name": vuln["plugin_name"],
            "score_source": vuln["source"],
            "severity": vuln["severity"],
            "vpr_score": vuln.get("vpr_score"),
        }

        if "cve" in vuln:
            # if we have a CVE, we can try to get the details from the NVD
            cve_doc = self.__db.CVEDoc.find_one({"_id": vuln["cve"]})
            if cve_doc:
                new_details["cvss_base_score"] = cve_doc["cvss_score"]
                new_details["cvss_version"] = cve_doc["cvss_version"]
                new_details["score_source"] = "nvd"
                new_details["severity"] = cve_doc["severity"]
            # if the CVE is listed in the KEV collection, we'll mark it as such
            kev_doc = self.__db.KEVDoc.find_one({"_id": vuln["cve"]})
            if kev_doc:
                new_details["kev"] = True

        # As of May 2022, some Nessus plugins report a severity that is
        # inconsistent with their (non-NVD, non-CVE-based) CVSS v3 score.
        # To reduce confusion, we ensure that the severity is correct here.
        # For examples, see the following plugins:
        # 34460, 104572, 107056, 140770, 156560, 156941, 156441
        if new_details["score_source"] != "nvd":
            cvss = new_details["cvss_base_score"]
            # Source: https://nvd.nist.gov/vuln-metrics/cvss
            #
            # Notes:
            # - The CVSS score to severity mapping is not continuous (e.g. a
            #   score of 8.95 is undefined according to their table).
            #   However, the CVSS equation documentation
            #   (https://www.first.org/cvss/specification-document#CVSS-v3-1-Equations)
            #   specifies that all CVSS scores are rounded up to the nearest
            #   tenth of a point, so our severity mapping below is valid.
            # - CVSSv3 specifies that a score of 0.0 has a severity of "None",
            #   but we have chosen to map 0.0 to severity 1 ("Low") because
            #   CyHy code has historically assumed severities between 1 and 4
            #   (inclusive).  Since we have not seen CVSSv3 scores lower than
            #   3.1, this will hopefully never be an issue.
            if new_details["cvss_version"] == "2":
                if cvss == 10:
                    new_details["severity"] = 4
                elif cvss >= 7.0:
                    new_details["severity"] = 3
                elif cvss >= 4.0:
                    new_details["severity"] = 2
                else:
                    new_details["severity"] = 1
            elif new_details["cvss_version"] == "3":
                if cvss >= 9.0:
                    new_details["severity"] = 4
                elif cvss >= 7.0:
                    new_details["severity"] = 3
                elif cvss >= 4.0:
                    new_details["severity"] = 2
                else:
                    new_details["severity"] = 1

        delta = []
        if check_for_changes:
            delta = self.__calculate_delta(ticket["details"], new_details)
            if delta:
                event = {
                    "action": TICKET_EVENT.CHANGED,
                    "delta": delta,
                    "reason": "details changed",
                    "reference": vuln["_id"],
                    "time": vuln["time"],
                }
                if self.__manual_scan:
                    event["manual"] = True
                ticket["events"].append(event)

        ticket["details"] = new_details
        return delta

    def __create_notification(self, ticket):
        """Create a notification from a ticket and save it in the database."""
        new_notification = self.__db.NotificationDoc()
        new_notification["ticket_id"] = ticket["_id"]
        new_notification["ticket_owner"] = ticket["owner"]
        # generated_for is initialized as an empty list.  Whenever a
        # notification PDF is generated using this NotificationDoc
        # (by cyhy-reports), the owner _id for that PDF is added to the
        # generated_for list.  It's a list because the same NotificationDoc
        # can get used in both a parent and a descendant PDF.
        new_notification["generated_for"] = list()
        new_notification.save()

    def open_ticket(self, vuln, reason):
        if self.__closing_time is None or self.__closing_time < vuln["time"]:
            self.__closing_time = vuln["time"]

        # search for previous open ticket that matches
        prev_open_ticket = self.__db.TicketDoc.find_one(
            {
                "ip_int": long(vuln["ip"]),
                "open": True,
                "port": vuln["port"],
                "protocol": vuln["protocol"],
                "source_id": vuln["plugin_id"],
                "source": vuln["source"],
            }
        )
        if prev_open_ticket:
            delta = self.__generate_ticket_details(vuln, prev_open_ticket)
            self.__check_false_positive_expiration(
                prev_open_ticket, vuln["time"].replace(tzinfo=tz.tzutc())
            )  # explicitly set to UTC (see CYHY-286)
            # add an entry to the existing open ticket
            event = {
                "action": TICKET_EVENT.VERIFIED,
                "reason": reason,
                "reference": vuln["_id"],
                "time": vuln["time"],
            }
            if self.__manual_scan:
                event["manual"] = True
            prev_open_ticket["events"].append(event)
            prev_open_ticket.save()
            self.__mark_seen(prev_open_ticket)

            # Create a notification for non-false positive tickets if:
            # - Severity delta goes from less than 3 (High) to 3 or greater
            # - KEV delta goes from False to True
            if not prev_open_ticket.get("false_positive"):
                for d in delta:
                    if d["key"] == "severity":
                        if d["from"] < 3 and d["to"] >= 3:
                            self.__create_notification(prev_open_ticket)
                            break
                    if d["key"] == "kev":
                        if d["from"] is False and d["to"] is True:
                            self.__create_notification(prev_open_ticket)
                            break
            return

        # no matching tickets are currently open
        # search for a previously closed ticket that was closed before the cutoff
        cutoff_date = util.utcnow() + self.__reopen_delta
        reopen_ticket = self.__db.TicketDoc.find_one(
            {
                "ip_int": long(vuln["ip"]),
                "open": False,
                "port": vuln["port"],
                "protocol": vuln["protocol"],
                "source_id": vuln["plugin_id"],
                "source": vuln["source"],
                "time_closed": {"$gt": cutoff_date},
            }
        )

        if reopen_ticket:
            delta = self.__generate_ticket_details(vuln, reopen_ticket)
            event = {
                "action": TICKET_EVENT.REOPENED,
                "reason": reason,
                "reference": vuln["_id"],
                "time": vuln["time"],
            }
            if self.__manual_scan:
                event["manual"] = True
            reopen_ticket["events"].append(event)
            reopen_ticket["open"] = True
            reopen_ticket["time_closed"] = None
            reopen_ticket.save()
            self.__mark_seen(reopen_ticket)

            # Create a notification if:
            # - Severity delta goes from less than 3 (High) to 3 or greater
            # - KEV delta goes from False to True
            for d in delta:
                if d["key"] == "severity":
                    if d["from"] < 3 and d["to"] >= 3:
                        self.__create_notification(reopen_ticket)
                        break
                if d["key"] == "kev":
                    if d["from"] is False and d["to"] is True:
                        self.__create_notification(reopen_ticket)
                        break
            return

        # time to open a new ticket
        new_ticket = self.__db.TicketDoc()
        new_ticket.ip = vuln["ip"]
        new_ticket["owner"] = vuln["owner"]
        new_ticket["port"] = vuln["port"]
        new_ticket["protocol"] = vuln["protocol"]
        new_ticket["source_id"] = vuln["plugin_id"]
        new_ticket["source"] = vuln["source"]
        new_ticket["time_opened"] = vuln["time"]
        self.__generate_ticket_details(vuln, new_ticket, check_for_changes=False)

        host = self.__db.HostDoc.get_by_ip(vuln["ip"])
        if host is not None:
            new_ticket["loc"] = host["loc"]

        event = {
            "action": TICKET_EVENT.OPENED,
            "reason": reason,
            "reference": vuln["_id"],
            "time": vuln["time"],
        }
        if self.__manual_scan:
            event["manual"] = True
        new_ticket["events"].append(event)

        if (
            new_ticket["owner"] == UNKNOWN_OWNER
        ):  # close tickets with no owner immediately
            event = {
                "action": TICKET_EVENT.CLOSED,
                "reason": "No associated owner",
                "reference": None,
                "time": vuln["time"],
            }
            if self.__manual_scan:
                event["manual"] = True
            new_ticket["events"].append(event)
            new_ticket["open"] = False
            new_ticket["time_closed"] = self.__closing_time

        new_ticket.save()
        self.__mark_seen(new_ticket)

        # Create notifications for Highs (3) or Criticals (4), or if KEV is true
        if new_ticket["details"]["severity"] > 2 or new_ticket["details"]["kev"]:
            self.__create_notification(new_ticket)

    def close_tickets(self):
        if self.__closing_time is None:
            # You don't have to go home but you can't stay here
            self.__closing_time = util.utcnow()
        ip_ints = [int(i) for i in self.__ips]

        # find tickets that are covered by this scan, but weren't just touched
        # TODO: this is the way I wanted to do it, but it blows up mongo
        # tickets = self.__db.TicketDoc.find({'ip_int':{'$in':ip_ints},
        #                                     'port':{'$in':self.__ports},
        #                                     'source_id':{'$in':self.__source_ids},
        #                                     '_id':{'$nin':list(self.__seen_ticket_ids)},
        #                                     'source':self.__source,
        #                                     'open':True})

        # work-around using a pipeline
        tickets = database.run_pipeline_cursor(
            close_tickets_pl(
                ip_ints,
                list(self.__ports),
                list(self.__source_ids),
                list(self.__seen_ticket_ids),
                self.__source,
            ),
            self.__db,
        )

        for raw_ticket in tickets:
            ticket = self.__db.TicketDoc(raw_ticket)  # make it managed
            # don't close tickets that are false_positives, just add event
            reason = "vulnerability not detected"
            self.__check_false_positive_expiration(
                ticket, self.__closing_time.replace(tzinfo=tz.tzutc())
            )  # explicitly set to UTC (see CYHY-286)
            if ticket["false_positive"] is True:
                event = {
                    "action": TICKET_EVENT.UNVERIFIED,
                    "reason": reason,
                    "reference": None,
                    "time": self.__closing_time,
                }
            else:
                ticket["open"] = False
                ticket["time_closed"] = self.__closing_time
                event = {
                    "action": TICKET_EVENT.CLOSED,
                    "reason": reason,
                    "reference": None,
                    "time": self.__closing_time,
                }
            if self.__manual_scan:
                event["manual"] = True
            ticket["events"].append(event)
            ticket.save()

    def ready_to_clear_vuln_latest_flags(self):
        return (
            len(self.__ips) > 0 and len(self.__ports) > 0 and len(self.__source_ids) > 0
        )

    def clear_vuln_latest_flags(self):
        """clear the latest flag for vuln_docs that match the ticket_manager scope"""
        ip_ints = [int(i) for i in self.__ips]
        pipeline = clear_latest_vulns_pl(
            ip_ints, list(self.__ports), list(self.__source_ids), self.__source
        )
        raw_vulns = database.run_pipeline_cursor(pipeline, self.__db)
        for raw_vuln in raw_vulns:
            vuln = self.__db.VulnScanDoc(raw_vuln)
            vuln["latest"] = False
            vuln.save()


class IPPortTicketManager(object):
    """Handles the opening and closing of tickets for a port scan (PORTSCAN)"""

    def __init__(self, db, protocols, reopen_days=90):
        self.__closing_time = None
        self.__db = db
        self.__ips = IPSet()  # ips that were scanned
        self.__ports = set()  # ports that were scanned
        self.__protocols = set(protocols)  # protocols that were scanned
        self.__reopen_delta = relativedelta.relativedelta(days=-reopen_days)
        self.__seen_ip_port = defaultdict(set)  # {ip:set({1,2,3}), ...}

    @property
    def ips(self):
        return self.__ips

    @ips.setter
    def ips(self, ips):
        self.__ips = IPSet(ips)

    @property
    def ports(self):
        return self.__ports

    @ports.setter
    def ports(self, ports):
        self.__ports = list(ports)

    def port_open(self, ip, port):
        self.__seen_ip_port[ip].add(port)

    def __check_false_positive_expiration(self, ticket, closing_time):
        # if false_positive expiration date has been reached,
        # flip false_positive flag and add CHANGED event
        if ticket["false_positive"] is True:
            fp_effective_date, fp_expiration_date = ticket.false_positive_dates
            if fp_expiration_date < closing_time:
                ticket["false_positive"] = False
                event = {
                    "action": TICKET_EVENT.CHANGED,
                    "delta": [{"from": True, "to": False, "key": "false_positive"}],
                    "reason": "False positive expired",
                    "reference": None,
                    "time": closing_time,
                }
                ticket["events"].append(event)

    def __handle_ticket_port_closed(self, ticket, closing_time):
        # don't close tickets that are false_positives, just add event
        reason = "port not open"
        self.__check_false_positive_expiration(
            ticket, closing_time.replace(tzinfo=tz.tzutc())
        )  # explicitly set to UTC (see CYHY-286)
        if ticket["false_positive"] is True:
            event = {
                "action": TICKET_EVENT.UNVERIFIED,
                "reason": reason,
                "reference": None,
                "time": closing_time,
            }
        else:
            ticket["open"] = False
            ticket["time_closed"] = closing_time
            event = {
                "action": TICKET_EVENT.CLOSED,
                "reason": reason,
                "reference": None,
                "time": closing_time,
            }
        ticket["events"].append(event)
        ticket.save()

    def __create_notification(self, ticket):
        """Create a notification from a ticket and save it in the database."""
        new_notification = self.__db.NotificationDoc()
        new_notification["ticket_id"] = ticket["_id"]
        new_notification["ticket_owner"] = ticket["owner"]
        # generated_for is initialized as an empty list.  Whenever a
        # notification PDF is generated using this NotificationDoc
        # (by cyhy-reports), the owner _id for that PDF is added to the
        # generated_for list.  It's a list because the same NotificationDoc
        # can get used in both a parent and a descendant PDF.
        new_notification["generated_for"] = list()
        new_notification.save()

    def open_ticket(self, portscan, reason):
        if self.__closing_time is None or self.__closing_time < portscan["time"]:
            self.__closing_time = portscan["time"]

        # search for previous open ticket that matches
        prev_open_ticket = self.__db.TicketDoc.find_one(
            {
                "ip_int": portscan["ip_int"],
                "open": True,
                "port": portscan["port"],
                "protocol": portscan["protocol"],
                "source_id": portscan["source_id"],
                "source": portscan["source"],
            }
        )
        if prev_open_ticket:
            self.__check_false_positive_expiration(
                prev_open_ticket, portscan["time"].replace(tzinfo=tz.tzutc())
            )  # explicitly set to UTC (see CYHY-286)
            # add an entry to the existing open ticket
            event = {
                "action": TICKET_EVENT.VERIFIED,
                "reason": reason,
                "reference": portscan["_id"],
                "time": portscan["time"],
            }
            prev_open_ticket["events"].append(event)
            prev_open_ticket.save()
            return

        # no matching tickets are currently open
        # search for a previously closed ticket that was closed before the cutoff
        cutoff_date = util.utcnow() + self.__reopen_delta
        reopen_ticket = self.__db.TicketDoc.find_one(
            {
                "ip_int": portscan["ip_int"],
                "open": False,
                "port": portscan["port"],
                "protocol": portscan["protocol"],
                "source_id": portscan["source_id"],
                "source": portscan["source"],
                "time_closed": {"$gt": cutoff_date},
            }
        )

        if reopen_ticket:
            event = {
                "action": TICKET_EVENT.REOPENED,
                "reason": reason,
                "reference": portscan["_id"],
                "time": portscan["time"],
            }
            reopen_ticket["events"].append(event)
            reopen_ticket["time_closed"] = None
            reopen_ticket["open"] = True
            reopen_ticket.save()
            return

        # time to open a new ticket
        new_ticket = self.__db.TicketDoc()
        new_ticket.ip = portscan["ip"]
        new_ticket["details"] = {
            "cve": None,
            "cvss_base_score": None,
            "name": portscan["name"],
            "score_source": None,
            "service": portscan["service"],
            "severity": 0,
        }
        new_ticket["owner"] = portscan["owner"]
        new_ticket["port"] = portscan["port"]
        new_ticket["protocol"] = portscan["protocol"]
        new_ticket["source_id"] = portscan["source_id"]
        new_ticket["source"] = portscan["source"]
        new_ticket["time_opened"] = portscan["time"]

        host = self.__db.HostDoc.get_by_ip(portscan["ip"])
        if host is not None:
            new_ticket["loc"] = host["loc"]

        event = {
            "action": TICKET_EVENT.OPENED,
            "reason": reason,
            "reference": portscan["_id"],
            "time": portscan["time"],
        }
        new_ticket["events"].append(event)

        if (
            new_ticket["owner"] == UNKNOWN_OWNER
        ):  # close tickets with no owner immediately
            event = {
                "action": TICKET_EVENT.CLOSED,
                "reason": "No associated owner",
                "reference": None,
                "time": portscan["time"],
            }
            new_ticket["events"].append(event)
            new_ticket["open"] = False
            new_ticket["time_closed"] = self.__closing_time

        new_ticket.save()

        # Create a notification for this ticket
        self.__create_notification(new_ticket)

    def close_tickets(self, closing_time=None):
        if closing_time is None:
            closing_time = util.utcnow()
        ip_ints = [int(i) for i in self.__ips]

        all_ports_scanned = len(self.__ports) == MAX_PORTS_COUNT

        if all_ports_scanned:
            # If all the ports were scanned we have an opportunity to close port 0
            # tickets. This can only be done if no ports are open for an IP.
            # Otherwise they can be closed in the VULNSCAN stage.
            ips_with_no_open_ports = self.__ips - IPSet(self.__seen_ip_port.keys())
            ips_with_no_open_ports_ints = [int(i) for i in ips_with_no_open_ports]

            # Close all tickets regardless of protocol for ips_with_no_open_ports
            tickets_to_close = self.__db.TicketDoc.find(
                {"ip_int": {"$in": ips_with_no_open_ports_ints}, "open": True}
            )

            for ticket in tickets_to_close:
                self.__handle_ticket_port_closed(ticket, closing_time)

            # handle ips that had at least one port open
            # next query optimized for all_ports_scanned
            tickets = self.__db.TicketDoc.find(
                {
                    "ip_int": {"$in": ip_ints},
                    "open": True,
                    "port": {"$ne": 0},
                    "protocol": {"$in": list(self.__protocols)},
                }
            )
        else:
            # not all ports scanned
            tickets = self.__db.TicketDoc.find(
                {
                    "ip_int": {"$in": ip_ints},
                    "open": True,
                    "port": {"$in": list(self.__ports)},
                    "protocol": {"$in": list(self.__protocols)},
                }
            )

        for ticket in tickets:
            if ticket["port"] in self.__seen_ip_port[ticket["ip"]]:
                # this ticket's ip:port was open, so we skip closing it
                continue
            self.__handle_ticket_port_closed(ticket, closing_time)

    def clear_vuln_latest_flags(self):
        """clear latest flags of vuln_docs that didn't have an associated open port"""
        ip_ints = [int(i) for i in self.__ips]

        # find vulns that are covered by this scan, but weren't just touched
        vuln_docs = self.__db.VulnScanDoc.find(
            {"ip_int": {"$in": ip_ints}, "latest": True}
        )
        for doc in vuln_docs:
            if doc["port"] not in self.__seen_ip_port[doc["ip"]]:
                # this doc's ip:port was not open, so we clear the latest flag
                doc["latest"] = False
                doc.save()


class IPTicketManager(object):
    """Handle the closing of tickets for a host scan (NETSCAN)."""

    def __init__(self, db):
        self.__db = db
        self.__ips = IPSet()  # ips that were scanned
        self.__seen_ips = IPSet()  # ips that were up

    @property
    def ips(self):
        return self.__ips

    @ips.setter
    def ips(self, ips):
        self.__ips = IPSet(ips)

    def ip_up(self, ip):
        self.__seen_ips.add(ip)

    def __check_false_positive_expiration(self, ticket, closing_time):
        # if false_positive expiration date has been reached,
        # flip false_positive flag and add CHANGED event
        if ticket["false_positive"] is True:
            fp_effective_date, fp_expiration_date = ticket.false_positive_dates
            if fp_expiration_date < closing_time:
                ticket["false_positive"] = False
                event = {
                    "action": TICKET_EVENT.CHANGED,
                    "delta": [{"from": True, "to": False, "key": "false_positive"}],
                    "reason": "False positive expired",
                    "reference": None,
                    "time": closing_time,
                }
                ticket["events"].append(event)

    def close_tickets(self, closing_time=None):
        if closing_time is None:
            closing_time = util.utcnow()

        not_up_ips = self.__ips - self.__seen_ips

        ip_ints = [int(i) for i in not_up_ips]

        # find tickets with ips that were not up and are open
        tickets = self.__db.TicketDoc.find({"ip_int": {"$in": ip_ints}, "open": True})

        for ticket in tickets:
            # don't close tickets that are false_positives, just add event
            reason = "host down"
            self.__check_false_positive_expiration(
                ticket, closing_time.replace(tzinfo=tz.tzutc())
            )  # explicitly set to UTC (see CYHY-286)
            if ticket["false_positive"] is True:
                event = {
                    "action": TICKET_EVENT.UNVERIFIED,
                    "reason": reason,
                    "reference": None,
                    "time": closing_time,
                }
            else:
                ticket["open"] = False
                ticket["time_closed"] = closing_time
                event = {
                    "action": TICKET_EVENT.CLOSED,
                    "reason": reason,
                    "reference": None,
                    "time": closing_time,
                }
            ticket["events"].append(event)
            ticket.save()

    def clear_vuln_latest_flags(self):
        """clear latest flags of vuln_docs that had IPs that were not up"""
        not_up_ips = self.__ips - self.__seen_ips
        ip_ints = [int(i) for i in not_up_ips]

        # find vulns that are covered by this scan, but weren't just touched
        vuln_docs = self.__db.VulnScanDoc.find(
            {"ip_int": {"$in": ip_ints}, "latest": True}
        )
        for doc in vuln_docs:
            doc["latest"] = False
            doc.save()
