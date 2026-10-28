-- A manual rung change on the Agents page is recorded as set_by 'owner'; older rows used other labels.
UPDATE agent_status SET set_by = 'owner' WHERE set_by NOT IN ('review', 'owner');
