# std
from collections import defaultdict

# project
from checks import AgentCheck

# 3rd party
from pysnmp.entity.rfc3413.oneliner import cmdgen
from pysnmp.smi.exval import noSuchInstance, noSuchObject
from pysnmp.smi import builder
import pysnmp.proto.rfc1902 as snmp_type

# Additional types that are not part of the SNMP protocol. cf RFC 2856
(CounterBasedGauge64, ZeroBasedCounter64) = builder.MibBuilder().importSymbols("HCNUM-TC",
                                                                               "CounterBasedGauge64",
                                                                               "ZeroBasedCounter64")

# Metric type that we support
SNMP_COUNTERS = [snmp_type.Counter32.__name__,
                 snmp_type.Counter64.__name__,
                 ZeroBasedCounter64.__name__]
SNMP_GAUGES = [snmp_type.Gauge32.__name__,
               snmp_type.Unsigned32.__name__,
               CounterBasedGauge64.__name__]

def reply_invalid(oid):
    return noSuchInstance.isSameTypeWith(oid) or \
           noSuchObject.isSameTypeWith(oid)

class SnmpCheck(AgentCheck):

    cmd_generator = None
    # pysnmp default values
    RETRIES = 5
    TIMEOUT = 1

    def __init__(self, name, init_config, agentConfig, instances=None):
        AgentCheck.__init__(self, name, init_config, agentConfig, instances)

        # Load Custom MIB directory
        mibs_path = None
        if init_config is not None:
            mibs_path = init_config.get("mibs_folder")
        SnmpCheck.create_command_generator(mibs_path)

    @classmethod
    def create_command_generator(cls, mibs_path=None):
        '''
        Create a command generator to perform all the snmp query.
        If mibs_path is not None, load the mibs present in the custom mibs
        folder. (Need to be in pysnmp format)
        '''
        cls.cmd_generator = cmdgen.CommandGenerator()
        if mibs_path is not None:
            mib_builder = cls.cmd_generator.snmpEngine.msgAndPduDsp.\
                          mibInstrumController.mibBuilder
            mib_sources = mib_builder.getMibSources() + (
                    builder.DirMibSource(mibs_path),
                    )
            mib_builder.setMibSources(*mib_sources)

    @classmethod
    def get_auth_data(cls, instance):
        '''
        Generate a Security Parameters object based on the instance's
        configuration.
        See http://pysnmp.sourceforge.net/docs/current/security-configuration.html
        '''
        if "community_string" in instance:
            # SNMP v1 - SNMP v2
            return cmdgen.CommunityData(instance['community_string'])
        elif "user" in instance:
            # SNMP v3
            user = instance["user"]
            auth_key = None
            priv_key = None
            auth_protocol = None
            priv_protocol = None
            if "authKey" in instance:
                auth_key = instance["authKey"]
                auth_protocol = cmdgen.usmHMACMD5AuthProtocol
            if "privKey" in instance:
                priv_key = instance["privKey"]
                auth_protocol = cmdgen.usmHMACMD5AuthProtocol
                priv_protocol = cmdgen.usmDESPrivProtocol
            if "authProtocol" in instance:
                auth_protocol = getattr(cmdgen, instance["authProtocol"])
            if "privProtocol" in instance:
                priv_protocol = getattr(cmdgen, instance["privProtocol"])
            return cmdgen.UsmUserData(user,
                                      auth_key,
                                      priv_key,
                                      auth_protocol,
                                      priv_protocol)
        else:
            raise Exception("An authentication method needs to be provided")

    @classmethod
    def get_transport_target(cls, instance, timeout, retries):
        '''
        Generate a Transport target object based on the instance's configuration
        '''
        if "ip_address" not in instance:
            raise Exception("An IP address needs to be specified")
        ip_address = instance["ip_address"]
        port = instance.get("port", 161) # Default SNMP port
        return cmdgen.UdpTransportTarget((ip_address, port), timeout=timeout, retries=retries)

    def check_table(self, instance, oids, lookup_names):
        '''
        Perform a snmpwalk on the domain specified by the oids, on the device
        configured in instance.
        lookup_names is a boolean to specify whether or not to use the mibs to
        resolve the name and values.

        Returns a dictionary:
        dict[oid/metric_name][row index] = value
        In case of scalar objects, the row index is just 0
        '''
        transport_target = self.get_transport_target(instance, self.TIMEOUT, self.RETRIES)
        auth_data = self.get_auth_data(instance)

        snmp_command = self.cmd_generator.nextCmd
        error_indication, error_status, error_index, var_binds = snmp_command(
            auth_data,
            transport_target,
            *oids,
            lookupValues = lookup_names,
            lookupNames = lookup_names
            )

        results = defaultdict(dict)
        if error_indication:
            message = "{0} for instance {1}".format(error_indication,
                                                          instance["ip_address"])
            instance["service_check_error"] = message
            raise Exception(message)
        else:
            if error_status:
                message = "{0} for instance {1}".format(error_status.prettyPrint(),
                                                              instance["ip_address"])
                instance["service_check_error"] = message
                self.log.warning(message)
            else:
                for table_row in var_binds:
                    for result_oid, value in table_row:
                        if lookup_names:
                            object = result_oid.getMibSymbol()
                            metric =  object[1]
                            indexes = object[2]
                            results[metric][indexes] = value
                        else:
                            oid = result_oid.asTuple()
                            matching = ".".join([str(i) for i in oid])
                            results[matching] = value

        return results

    def check(self, instance):
        '''
        Perform two series of SNMP requests, one for all that have MIB asociated
        and should be looked up and one for those specified by oids
        '''
        tags = instance.get("tags",[])
        ip_address = instance["ip_address"]
        table_oids = []
        raw_oids = []

        # Check the metrics completely defined
        for metric in instance.get('metrics', []):
            if 'MIB' in metric:
                try:
                    assert "table" in metric or "symbol" in metric
                    to_query = metric.get("table", metric.get("symbol"))
                    table_oids.append(cmdgen.MibVariable(metric["MIB"], to_query))
                except Exception as e:
                    self.log.warning("Can't generate MIB object for variable : %s\n"
                                     "Exception: %s", metric, e)
            elif 'OID' in metric:
                if metric['OID'].endswith('.0'):
                    # oid containing the .0 index, as it's a scalar
                    # because we are querying using getnext, we need to remove it
                    self.log.debug("Removing the trailing .0 in the oid")
                    raw_oids.append(metric['OID'][:-2])
                else:
                    raw_oids.append(metric['OID'])
            else:
                raise Exception('Unsupported metric in config file: %s' % metric)
        try:
            if table_oids:
                self.log.debug("Querying device %s for %s oids", ip_address, len(table_oids))
                table_results = self.check_table(instance, table_oids, True)
                self.report_table_metrics(instance, table_results)

            if raw_oids:
                self.log.debug("Querying device %s for %s oids", ip_address, len(raw_oids))
                raw_results = self.check_table(instance, raw_oids, False)
                self.report_raw_metrics(instance, raw_results)
        except Exception as e:
            if "service_check_error" not in instance:
                instance["service_check_error"] = "Fail to collect metrics: {0}".format(e)
            raise
        finally:
            # Report service checks
            service_check_name = "snmp.can_check"
            tags = ["snmp_device:%s" % ip_address]
            if "service_check_error" in instance:
                self.service_check(service_check_name, AgentCheck.CRITICAL, tags=tags,
                    message=instance["service_check_error"])
            else:
                self.service_check(service_check_name, AgentCheck.OK, tags=tags)

    def report_raw_metrics(self, instance, results):
        '''
        For all the metrics that are specified as oid,
        the conf oid is going to be a prefix of the oid sent back by the device
        Use the instance configuration to find the name to give to the metric

        Submit the results to the aggregator.
        '''
        tags = instance.get("tags", [])
        tags = tags + ["snmp_device:"+instance.get('ip_address')]
        for metric in instance.get('metrics', []):
            if 'OID' in metric:
                queried_oid = metric['OID']
                for oid in results:
                    if oid.startswith(queried_oid):
                        value = results[oid]
                        break
                else:
                    self.log.warning("No matching results found for oid %s",
                                                                  queried_oid)
                    continue
                name = metric.get('name','unnamed_metric')
                self.submit_metric(name, value, tags)

    def report_table_metrics(self, instance, results):
        '''
        For each of the metrics specified as needing to be resolved with mib,
        gather the tags requested in the instance conf for each row.

        Submit the results to the aggregator.
        '''
        tags = instance.get("tags", [])
        tags = tags + ["snmp_device:"+instance.get('ip_address')]

        for metric in instance.get('metrics', []):
            if 'table' in metric:
                index_based_tags = []
                column_based_tags = []
                for metric_tag in metric.get('metric_tags', []):
                    tag_key = metric_tag['tag']
                    if 'index' in metric_tag:
                        index_based_tags.append((tag_key, metric_tag.get('index')))
                    elif 'column' in metric_tag:
                        column_based_tags.append((tag_key, metric_tag.get('column')))
                    else:
                        self.log.warning("No indication on what value to use for this tag")

                for value_to_collect in metric.get("symbols", []):
                    for index, val in results[value_to_collect].items():
                        metric_tags = tags + self.get_index_tags(index, results,
                                                                 index_based_tags,
                                                                 column_based_tags)
                        self.submit_metric(value_to_collect, val, metric_tags)

            elif 'symbol' in metric:
                name = metric['symbol']
                result = results[name].items()
                if len(result) > 1:
                    self.log("Several rows corresponding while the metric is supposed to be a scalar")
                    continue
                val = result[0][1]
                self.submit_metric(name, val, tags)
            elif 'OID' in metric:
                pass # This one is already handled by the other batch of requests
            else:
                raise Exception('Unsupported metric in config file: %s' % metric)

    def get_index_tags(self, index, results, index_tags, column_tags):
        '''
        Gather the tags for this row of the table (index) based on the
        results (all the results from the query).
        index_tags and column_tags are the tags to gather.
         - Those specified in index_tags contain the tag_group name and the
           index of the value we want to extract from the index tuple.
           cf. 1 for ipVersion in the IP-MIB::ipSystemStatsTable for example
         - Those specified in column_tags contain the name of a column, which
           could be a potential result, to use as a tage
           cf. ifDescr in the IF-MIB::ifTable for example
        '''
        tags = []
        for idx_tag in index_tags:
            tag_group = idx_tag[0]
            try:
                tag_value = index[idx_tag[1] - 1].prettyPrint()
            except IndexError:
                self.log.warning("Not enough indexes, skipping this tag")
                continue
            tags.append("{0}:{1}".format(tag_group, tag_value))
        for col_tag in column_tags:
            tag_group = col_tag[0]
            try:
                tag_value = results[col_tag[1]][index]
            except KeyError:
                self.log.warning("Column %s not present in the table, skipping this tag", col_tag[1])
                continue
            if reply_invalid(tag_value):
                self.log.warning("Can't deduct tag from column for tag %s",
                                 tag_group)
                continue
            tag_value = tag_value.prettyPrint()
            tags.append("{0}:{1}".format(tag_group, tag_value))
        return tags

    def submit_metric(self, name, snmp_value, tags=[]):
        '''
        Convert the values reported as pysnmp-Managed Objects to values and
        report them to the aggregator
        '''
        if reply_invalid(snmp_value):
            # Metrics not present in the queried object
            self.log.warning("No such Mib available: %s" % name)
            return

        metric_name = self.normalize(name, prefix="snmp")

        # Ugly hack but couldn't find a cleaner way
        # Proper way would be to use the ASN1 method isSameTypeWith but it
        # wrongfully returns True in the case of CounterBasedGauge64
        # and Counter64 for example
        snmp_class = snmp_value.__class__.__name__
        for counter_class in SNMP_COUNTERS:
            if snmp_class==counter_class:
                value = int(snmp_value)
                self.rate(metric_name, value, tags)
                return
        for gauge_class in SNMP_GAUGES:
            if snmp_class==gauge_class:
                value = int(snmp_value)
                self.gauge(metric_name, value, tags)
                return

        self.log.warning("Unsupported metric type %s", snmp_class)

