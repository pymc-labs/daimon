# Allowances

An allowance limits what a person may spend, in billed dollars: the cost of their
turns with the [markup](billing.md#pricing) applied. It is the third
[gate](billing.md#the-gates) a turn passes.

## What an allowance covers

An allowance names one of these:

- a person
- a group
- the invite a visitor came in by

It can name one agent too, and then limits spend on that agent alone. It counts
either the current UTC calendar month or everything since it started.

Admins add, change and remove allowances under **Credits** in the web app.

## Which allowance governs

The most specific allowance that covers a person governs them: their own, then
their invite's, then a group's. Of several of that kind, the one with the most
left wins. A member with no allowance is not limited. A visitor with none is
blocked.

## From the command line

`daimon tenants cap` sets one monthly allowance for everyone, or with `--user`
for one person. A zero amount blocks every turn. See
[Billing](billing.md#signup-credit-and-top-ups).
