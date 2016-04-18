# Copyright (c) 2016 Ansible, Inc.
# All Rights Reserved.

# Python
import logging
import threading
import contextlib

# Django
from django.db import models, transaction, connection
from django.db.models import Q
from django.core.urlresolvers import reverse
from django.utils.translation import ugettext_lazy as _
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey

# AWX
from django.contrib.auth.models import User # noqa
from awx.main.models.base import * # noqa

__all__ = [
    'Role',
    'batch_role_ancestor_rebuilding',
    'get_roles_on_resource',
    'ROLE_SINGLETON_SYSTEM_ADMINISTRATOR',
    'ROLE_SINGLETON_SYSTEM_AUDITOR',
]

logger = logging.getLogger('awx.main.models.rbac')

ROLE_SINGLETON_SYSTEM_ADMINISTRATOR='System Administrator'
ROLE_SINGLETON_SYSTEM_AUDITOR='System Auditor'

tls = threading.local() # thread local storage

@contextlib.contextmanager
def batch_role_ancestor_rebuilding(allow_nesting=False):
    '''
    Batches the role ancestor rebuild work necessary whenever role-role
    relations change. This can result in a big speedup when performing
    any bulk manipulation.

    WARNING: Calls to anything related to checking access/permissions
    while within the context of the batch_role_ancestor_rebuilding will
    likely not work.
    '''

    batch_role_rebuilding = getattr(tls, 'batch_role_rebuilding', False)

    try:
        setattr(tls, 'batch_role_rebuilding', True)
        if not batch_role_rebuilding:
            setattr(tls, 'roles_needing_rebuilding', set())
        yield

    finally:
        setattr(tls, 'batch_role_rebuilding', batch_role_rebuilding)
        if not batch_role_rebuilding:
            rebuild_set = getattr(tls, 'roles_needing_rebuilding')
            with transaction.atomic():
                Role._simultaneous_ancestry_rebuild(rebuild_set)

                #for role in Role.objects.filter(id__in=list(rebuild_set)).all():
                #    # TODO: We can reduce this to one rebuild call with our new upcoming rebuild method.. do this
                #    role.rebuild_role_ancestor_list()
            delattr(tls, 'roles_needing_rebuilding')


class Role(CommonModelNameNotUnique):
    '''
    Role model
    '''

    class Meta:
        app_label = 'main'
        verbose_name_plural = _('roles')
        db_table = 'main_rbac_roles'

    singleton_name = models.TextField(null=True, default=None, db_index=True, unique=True)
    role_field = models.TextField(null=False, default='')
    parents = models.ManyToManyField('Role', related_name='children')
    implicit_parents = models.TextField(null=False, default='[]')
    ancestors = models.ManyToManyField(
        'Role',
        through='RoleAncestorEntry',
        through_fields=('descendent', 'ancestor'),
        related_name='descendents'
    ) # auto-generated by `rebuild_role_ancestor_list`
    members = models.ManyToManyField('auth.User', related_name='roles')
    content_type = models.ForeignKey(ContentType, null=True, default=None)
    object_id = models.PositiveIntegerField(null=True, default=None)
    content_object = GenericForeignKey('content_type', 'object_id')

    def save(self, *args, **kwargs):
        super(Role, self).save(*args, **kwargs)
        self.rebuild_role_ancestor_list()

    def get_absolute_url(self):
        return reverse('api:role_detail', args=(self.pk,))

    def __contains__(self, accessor):
        if type(accessor) == User:
            return self.ancestors.filter(members=accessor).exists()
        elif accessor.__class__.__name__ == 'Team':
            return self.ancestors.filter(pk=accessor.member_role.id).exists()
        elif type(accessor) == Role:
            return self.ancestors.filter(pk=accessor).exists()
        else:
            accessor_type = ContentType.objects.get_for_model(accessor)
            roles = Role.objects.filter(content_type__pk=accessor_type.id,
                                        object_id=accessor.id)
            return self.ancestors.filter(pk__in=roles).exists()

    def rebuild_role_ancestor_list(self):
        '''
        Updates our `ancestors` map to accurately reflect all of the ancestors for a role

        You should never need to call this. Signal handlers should be calling
        this method when the role hierachy changes automatically.

        Note that this method relies on any parents' ancestor list being correct.
        '''
        global tls
        batch_role_rebuilding = getattr(tls, 'batch_role_rebuilding', False)

        if batch_role_rebuilding:
            roles_needing_rebuilding = getattr(tls, 'roles_needing_rebuilding')
            roles_needing_rebuilding.add(self.id)
            return

        #actual_ancestors = set(Role.objects.filter(id=self.id).values_list('parents__ancestors__id', flat=True))
        #actual_ancestors.add(self.id)
        #if None in actual_ancestors:
        #    actual_ancestors.remove(None)
        #stored_ancestors = set(self.ancestors.all().values_list('id', flat=True))

        '''
        # If it differs, update, and then update all of our children
        if actual_ancestors != stored_ancestors:
            for id in actual_ancestors - stored_ancestors:
                self.ancestors.add(id)
            for id in stored_ancestors - actual_ancestors:
                self.ancestors.remove(id)

            for child in self.children.all():
                child.rebuild_role_ancestor_list()
        '''

        # If our role heirarchy hasn't actually changed, don't do anything
        #if actual_ancestors == stored_ancestors:
        #    return

        Role._simultaneous_ancestry_rebuild([self.id])


    @staticmethod
    def _simultaneous_ancestry_rebuild(role_ids_to_rebuild):
        #
        # The simple version of what this function is doing
        # =================================================
        #
        #   When something changes in our role "hierarchy", we need to update
        #   the `Role.ancestors` mapping to reflect these changes. The basic
        #   idea, which the code in this method is modeled after, is to do
        #   this: When a change happens to a role's parents list, we update
        #   that role's ancestry list, then we recursively update any child
        #   roles ancestry lists.  Because our role relationships are not
        #   strictly hierarchical, and can even have loops, this process may
        #   necessarily visit the same nodes more than once. To handle this
        #   without having to keep track of what should be updated (again) and
        #   in what order, we simply use the termination condition of stopping
        #   when our stored ancestry list matches what our list should be, eg,
        #   when nothing changes. This can be simply implemented:
        #
        #      if actual_ancestors != stored_ancestors:
        #          for id in actual_ancestors - stored_ancestors:
        #              self.ancestors.add(id)
        #          for id in stored_ancestors - actual_ancestors:
        #              self.ancestors.remove(id)
        #
        #          for child in self.children.all():
        #              child.rebuild_role_ancestor_list()
        #
        #   However this results in a lot of calls to the database, so the
        #   optimized implementation below effectively does this same thing,
        #   but we update all children at once, so effectively we sweep down
        #   through our hierarchy one layer at a time instead of one node at a
        #   time. Because of how this method works, we can also start from many
        #   roots at once and sweep down a large set of roles, which we take
        #   advantage of when performing bulk operations.
        #
        #
        # SQL Breakdown
        # =============
        #   The Role ancestors has three columns, (id, from_role_id, to_role_id)
        #
        #      id:           Unqiue row ID
        #      from_role_id: Descendent role ID
        #      to_role_id:   Ancestor role ID
        #
        #      *NOTE* In addition to mapping roles to parents, there also
        #      always exists must exist an entry where
        #
        #           from_role_id == role_id == to_role_id
        #
        #      this makes our joins simple when we go to derive permissions or
        #      accessible objects.
        #
        #
        #   We operate under the assumption that our parent's ancestor list is
        #   correct, thus we can always compute what our ancestor list should
        #   be by taking the union of our parent's ancestor lists and adding
        #   our self reference entry from_role_id == role_id == to_role_id
        #
        #   The inner query for the two SQL statements compute this union,
        #   the union of the parent's ancestors and the self referncing entry,
        #   for all roles in the current set of roles to rebuild.
        #
        #   The DELETE query uses this to select all entries on disk for the
        #   roles we're dealing with, and removes the entries that are not in
        #   this list.
        #
        #   The INSERT query uses this to select all entries in the list that
        #   are not in the database yet, and inserts all of the missing
        #   records.
        #
        #   Once complete, we select all of the children for the roles we are
        #   working with, this list becomes the new role list we are working
        #   with.
        #
        #   When our delete or insert query return that they have not performed
        #   any work, then we know that our children will also not need to be
        #   updated, and so we can terminate our loop.
        #
        #

        if len(role_ids_to_rebuild) == 0:
            return

        cursor = connection.cursor()
        loop_ct = 0

        sql_params = {
            'ancestors_table': Role.ancestors.through._meta.db_table,
            'parents_table': Role.parents.through._meta.db_table,
            'roles_table': Role._meta.db_table,
            'ids': ','.join(str(x) for x in role_ids_to_rebuild)
        }

        # This is our solution for dealing with updates to parents of nodes
        # that are in cycles. It seems like we should be able to find a more
        # clever way of just dealing with the issue in another way, but it's
        # surefire and I'm not seeing the easy solution to dealing with that
        # problem that's not this.

        # TODO: Test to see if not deleting any entry that has a direct
        # correponding entry in the parents table helps reduce the processing
        # time significantly
        """
        cursor.execute('''
            DELETE FROM %(ancestors_table)s
              WHERE ancestor_id IN (%(ids)s)
                    AND descendent_id != ancestor_id
        ''' % sql_params)
        """
        cursor.execute('''
            DELETE FROM %(ancestors_table)s
              WHERE ancestor_id IN (%(ids)s)
        ''' % sql_params)


        while role_ids_to_rebuild:
            if loop_ct > 1000:
                raise Exception('Ancestry role rebuilding error: infinite loop detected')
            loop_ct += 1

            sql_params = {
                'ancestors_table': Role.ancestors.through._meta.db_table,
                'parents_table': Role.parents.through._meta.db_table,
                'roles_table': Role._meta.db_table,
                'ids': ','.join(str(x) for x in role_ids_to_rebuild)
            }

            delete_ct = 0

            cursor.execute('''
                DELETE FROM %(ancestors_table)s
                WHERE descendent_id IN (%(ids)s)
                      AND
                      id NOT IN (
                          SELECT %(ancestors_table)s.id FROM  (
                                SELECT parents.from_role_id from_id, ancestors.ancestor_id to_id
                                  FROM %(parents_table)s as parents
                                       LEFT JOIN %(ancestors_table)s as ancestors
                                           ON (parents.to_role_id = ancestors.descendent_id)
                                 WHERE parents.from_role_id IN (%(ids)s) AND ancestors.ancestor_id IS NOT NULL

                                 UNION

                                 SELECT id from_id, id to_id from %(roles_table)s WHERE id IN (%(ids)s)
                           ) new_ancestry_list
                           LEFT JOIN %(ancestors_table)s ON (new_ancestry_list.from_id = %(ancestors_table)s.descendent_id
                                                                   AND new_ancestry_list.to_id = %(ancestors_table)s.ancestor_id)
                           WHERE %(ancestors_table)s.id IS NOT NULL
                     )
            ''' % sql_params)
            delete_ct = cursor.rowcount

            cursor.execute('''
                INSERT INTO %(ancestors_table)s (descendent_id, ancestor_id, role_field, content_type_id, object_id)
                SELECT from_id, to_id, new_ancestry_list.role_field, new_ancestry_list.content_type_id, new_ancestry_list.object_id FROM  (
                      SELECT parents.from_role_id from_id,
                             ancestors.ancestor_id to_id,
                             roles.role_field,
                             COALESCE(roles.content_type_id, 0) content_type_id,
                             COALESCE(roles.object_id, 0) object_id
                        FROM %(parents_table)s as parents
                             INNER JOIN %(roles_table)s as roles ON (parents.from_role_id = roles.id)
                             LEFT OUTER JOIN %(ancestors_table)s as ancestors
                                 ON (parents.to_role_id = ancestors.descendent_id)
                       WHERE parents.from_role_id IN (%(ids)s) AND ancestors.ancestor_id IS NOT NULL

                       UNION

                      SELECT id from_id,
                             id to_id,
                             role_field,
                             COALESCE(content_type_id, 0) content_type_id,
                             COALESCE(object_id, 0) object_id
                       from %(roles_table)s WHERE id IN (%(ids)s)
                 ) new_ancestry_list
                 LEFT JOIN %(ancestors_table)s ON (new_ancestry_list.from_id = %(ancestors_table)s.descendent_id
                                                         AND new_ancestry_list.to_id = %(ancestors_table)s.ancestor_id)
                 WHERE %(ancestors_table)s.id IS NULL
            ''' % sql_params)
            insert_ct = cursor.rowcount

            if insert_ct == 0 and delete_ct == 0:
                break

            role_ids_to_rebuild = Role.objects.distinct() \
                                      .filter(id__in=role_ids_to_rebuild, children__id__isnull=False) \
                                      .values_list('children__id', flat=True)



    @staticmethod
    def visible_roles(user):
        return Role.objects.filter(Q(descendents__in=user.roles.filter()) | Q(ancestors__in=user.roles.filter()))

    @staticmethod
    def singleton(name):
        role, _ = Role.objects.get_or_create(singleton_name=name, name=name)
        return role

    def is_ancestor_of(self, role):
        return role.ancestors.filter(id=self.id).exists()

class RoleAncestorEntry(models.Model):

    class Meta:
        app_label = 'main'
        verbose_name_plural = _('role_ancestors')
        db_table = 'main_rbac_role_ancestors'
        index_together = [
            ("ancestor", "content_type_id", "object_id"),     # used by get_roles_on_resource
            ("ancestor", "content_type_id", "role_field"),    # used by accessible_objects
        ]

    descendent      = models.ForeignKey(Role, null=False, on_delete=models.CASCADE, related_name='+')
    ancestor        = models.ForeignKey(Role, null=False, on_delete=models.CASCADE, related_name='+')
    role_field      = models.TextField(null=False)
    #content_type_id = models.PositiveIntegerField(null=False)
    #object_id       = models.PositiveIntegerField(null=False)
    content_type_id = models.PositiveIntegerField(null=False)
    object_id       = models.PositiveIntegerField(null=False)


def get_roles_on_resource(resource, accessor):
    '''
    Returns a dict (or None) of the roles a accessor has for a given resource.
    An accessor can be either a User, Role, or an arbitrary resource that
    contains one or more Roles associated with it.
    '''

    if type(accessor) == User:
        roles = accessor.roles.all()
    elif type(accessor) == Role:
        roles = [accessor]
    else:
        accessor_type = ContentType.objects.get_for_model(accessor)
        roles = Role.objects.filter(content_type__pk=accessor_type.id,
                                    object_id=accessor.id)

    return {
        role_field: True for role_field in
        RoleAncestorEntry.objects.filter(
            ancestor__in=roles,
            content_type_id=ContentType.objects.get_for_model(resource).id,
            object_id=resource.id
        ).values_list('role_field', flat=True)
    }

