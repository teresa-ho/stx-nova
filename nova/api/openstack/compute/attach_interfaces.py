# Copyright 2012 SINA Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# Copyright (c) 2016-2017 Wind River Systems, Inc.
#

"""The instance interfaces extension."""

import webob
from webob import exc

from nova.api.openstack import common
from nova.api.openstack.compute.schemas import attach_interfaces
from nova.api.openstack.compute import wrs_server_if
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.api import validation
from nova import compute
from nova import exception
from nova.i18n import _
from nova import network
from nova.network import model as network_model
from nova.network.neutronv2 import api as neutronapi
from nova import objects
from nova.policies import attach_interfaces as ai_policies


def _translate_interface_attachment_view(port_info):
    """Maps keys for interface attachment details view."""
    return {
        'net_id': port_info['network_id'],
        'port_id': port_info['id'],
        'mac_addr': port_info['mac_address'],
        'port_state': port_info['status'],
        'fixed_ips': port_info.get('fixed_ips', None),
        }


class InterfaceAttachmentController(wsgi.Controller):
    """The interface attachment API controller for the OpenStack API."""

    def __init__(self):
        self.compute_api = compute.API()
        self.network_api = network.API()
        super(InterfaceAttachmentController, self).__init__()

    @extensions.expected_errors((404, 501))
    def index(self, req, server_id):
        """Returns the list of interface attachments for a given instance."""
        context = req.environ['nova.context']
        context.can(ai_policies.BASE_POLICY_NAME)

        instance = common.get_instance(self.compute_api, context, server_id)
        search_opts = {'device_id': instance.uuid}

        try:
            data = self.network_api.list_ports(context, **search_opts)
        except exception.NotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())
        except NotImplementedError:
            common.raise_feature_not_supported()

        ports = data.get('ports', [])
        entity_maker = _translate_interface_attachment_view
        results = [entity_maker(port) for port in ports]

        return {'interfaceAttachments': results}

    @extensions.expected_errors((403, 404))
    def show(self, req, server_id, id):
        """Return data about the given interface attachment."""
        context = req.environ['nova.context']
        context.can(ai_policies.BASE_POLICY_NAME)

        port_id = id
        # NOTE(mriedem): We need to verify the instance actually exists from
        # the server_id even though we're not using the instance for anything,
        # just the port id.
        common.get_instance(self.compute_api, context, server_id)

        try:
            port_info = self.network_api.show_port(context, port_id)
        except exception.PortNotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())
        except exception.Forbidden as e:
            raise exc.HTTPForbidden(explanation=e.format_message())

        if port_info['port']['device_id'] != server_id:
            msg = _("Instance %(instance)s does not have a port with id "
                    "%(port)s") % {'instance': server_id, 'port': port_id}
            raise exc.HTTPNotFound(explanation=msg)

        return {'interfaceAttachment': _translate_interface_attachment_view(
                port_info['port'])}

    @extensions.block_during_upgrade()
    @extensions.expected_errors((400, 404, 409, 500, 501))
    @validation.schema(attach_interfaces.create, '2.0', '2.48')
    @validation.schema(attach_interfaces.create_v249, '2.49')
    def create(self, req, server_id, body):
        """Attach an interface to an instance."""
        context = req.environ['nova.context']
        context.can(ai_policies.BASE_POLICY_NAME)
        context.can(ai_policies.POLICY_ROOT % 'create')

        network_id = None
        port_id = None
        req_ip = None
        tag = None
        vif_model = None
        if body:
            attachment = body['interfaceAttachment']
            network_id = attachment.get('net_id', None)
            port_id = attachment.get('port_id', None)
            tag = attachment.get('tag', None)
            vif_model = attachment.get('wrs-if:vif_model')
            try:
                req_ip = attachment['fixed_ips'][0]['ip_address']
            except Exception:
                pass

        if network_id and port_id:
            msg = _("Must not input both network_id and port_id")
            raise exc.HTTPBadRequest(explanation=msg)
        if req_ip and not network_id:
            msg = _("Must input network_id when request IP address")
            raise exc.HTTPBadRequest(explanation=msg)

        instance = common.get_instance(self.compute_api, context, server_id)

        try:
            if hasattr(instance, 'info_cache'):
                instance_nic_count = len(instance.info_cache.network_info)
                if instance_nic_count >= wrs_server_if.MAXIMUM_VNICS:
                    msg = _("Already at %(max)d configured NICs, "
                            "which is the maximum amount supported") % {
                                "max": wrs_server_if.MAXIMUM_VNICS,
                           }
                    raise exc.HTTPBadRequest(explanation=msg)
            if hasattr(instance, 'host'):
                physkey = 'provider:physical_network'
                aggr_list = objects.AggregateList.get_by_metadata_key(
                    context, physkey, hosts=set([instance.host])
                )
                providernet_list = []
                for aggr_entry in aggr_list:
                    providernet_list.append(aggr_entry.metadata[physkey])

                neutron = neutronapi.get_client(context, admin=True)
                temp_network_id = None
                if port_id:
                    port = neutron.show_port(port_id)['port']
                    temp_network_id = port.get('network_id')
                    if not vif_model:
                        vif_model = port.get('wrs-binding:vif_model')
                else:
                    temp_network_id = network_id
                network = neutron.show_network(temp_network_id)['network']
                providernet = network[physkey]
                providernet_on_host = (providernet in providernet_list)
                if not providernet_on_host:
                    msg = _("Providernet %(pnet)s not on instance's "
                            "host %(host)s") % {
                                "pnet": providernet,
                                "host": instance.host,
                            }
                    raise exc.HTTPBadRequest(explanation=msg)
            if not vif_model:
                vif_model = network_model.VIF_MODEL_VIRTIO
            elif vif_model not in network_model.VIF_MODEL_HOTPLUGGABLE:
                msg = _("Interface attach not supported for vif_model "
                        "%(vif_model)s. Must be one of %(valid_vifs)s.") % {
                            'vif_model': vif_model,
                            'valid_vifs': network_model.VIF_MODEL_HOTPLUGGABLE,
                        }
                raise exception.InvalidInput(msg)
            vif = self.compute_api.attach_interface(context,
                instance, network_id, port_id, req_ip, vif_model, tag=tag)
        except (exception.InterfaceAttachFailedNoNetwork,
                exception.NetworkAmbiguous,
                exception.NoMoreFixedIps,
                exception.PortNotUsable,
                exception.AttachInterfaceNotSupported,
                exception.SecurityGroupCannotBeApplied,
                exception.InvalidInput,
                exception.TaggedAttachmentNotSupported) as e:
            raise exc.HTTPBadRequest(explanation=e.format_message())
        except (exception.InstanceIsLocked,
                exception.FixedIpAlreadyInUse,
                exception.PortInUse) as e:
            raise exc.HTTPConflict(explanation=e.format_message())
        except (exception.PortNotFound,
                exception.NetworkNotFound) as e:
            raise exc.HTTPNotFound(explanation=e.format_message())
        except exception.InterfaceAttachFailed as e:
            raise webob.exc.HTTPInternalServerError(
                explanation=e.format_message())
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(state_error,
                    'attach_interface', server_id)

        return self.show(req, server_id, vif['id'])

    @extensions.block_during_upgrade()
    @wsgi.response(202)
    @extensions.expected_errors((404, 409, 501))
    def delete(self, req, server_id, id):
        """Detach an interface from an instance."""
        context = req.environ['nova.context']
        context.can(ai_policies.BASE_POLICY_NAME)
        context.can(ai_policies.POLICY_ROOT % 'delete')
        port_id = id

        instance = common.get_instance(self.compute_api, context, server_id,
                                       expected_attrs=['device_metadata'])
        try:
            self.compute_api.detach_interface(context,
                instance, port_id=port_id)
        except exception.PortNotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())
        except exception.InstanceIsLocked as e:
            raise exc.HTTPConflict(explanation=e.format_message())
        except NotImplementedError:
            common.raise_feature_not_supported()
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(state_error,
                    'detach_interface', server_id)
