- **Lead intake records a long email that is not a lead.** An email could be longer than a
  record can hold: an ordinary HTML newsletter passes 100,000 characters. Such a message could
  not be submitted, even as "not a lead", so it stayed discovered and came back on every scan,
  and an hourly routine reported the same problem each time. A message classified as not a lead
  is still read whole, and its record now keeps the first 100,000 characters. A lead that long is
  still refused, so that its sender and quotes are never checked on part of it
  ([Lead intake](docs/lead-intake.md#qualification-and-delivery)).
