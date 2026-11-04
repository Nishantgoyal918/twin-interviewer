-- Twins made by the interview simulator (a persona agent took the interview, not a person).
-- In the open demo they sit in the shared space with everyone's twins, labelled "simulated".
alter table twins add column if not exists simulated boolean not null default false;
