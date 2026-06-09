	// GetTable returns `namespace` as a list; bridge the first element back into
	// the scalar Spec.Namespace so observed state matches desired.
	if len(resp.Namespace) > 0 {
		ko.Spec.Namespace = &resp.Namespace[0]
	}
	// GetTable does not return tags; fetch them via ListTagsForResource so the
	// delta against Spec.Tags is accurate and we avoid spurious deltas.
	if err := rm.setResourceTags(ctx, ko); err != nil {
		return nil, err
	}
