from capture_progress import CaptureProgress


def test_eta_uses_newly_processed_records_and_updates_as_inventory_grows():
    instant = [0]
    tracker = CaptureProgress(clock=lambda: instant[0])
    def report(total, remaining, phase='downloading'):
        return tracker.update({'phase': phase, 'versions_found': total, 'versions_pending': remaining})['completion']
    first = report(200, 150)
    assert (first['completed'], first['remaining'], first['eta_seconds']) == (50, 150, None)
    instant[0] = 30
    second = report(200, 140)
    assert second['eta_seconds'] == 420  # saved/reused 50 do not inflate throughput
    instant[0] = 60
    grown = report(300, 230)
    assert (grown['total'], grown['completed'], grown['remaining'], grown['eta_seconds']) == (300, 70, 230, 690)
    instant[0] = 90
    assert report(300, 230)['eta_seconds'] == 1035  # slow responses increase the estimate
    assert report(300, 0, 'ready_for_review')['eta_seconds'] is None


def test_resume_and_board_to_supporting_files_reset_eta_without_losing_counts():
    instant = [0]
    tracker = CaptureProgress(clock=lambda: instant[0])
    def board(captured, pending, downloaded=0):
        return {'phase':'downloading', 'ezboard':{'counts':{'captured':captured,'pending':pending,'downloaded':downloaded,'excluded':2}}}
    tracker.update(board(40, 20, 1))
    instant[0] = 10
    result = tracker.update(board(42, 19))['completion']
    assert (result['total'], result['remaining'], result['eta_seconds']) == (63, 19, 95)
    instant[0] = 3600
    resumed = CaptureProgress(clock=lambda: instant[0]).update(board(42, 19))['completion']
    assert resumed['completed'] == 44 and resumed['eta_seconds'] is None
    files = tracker.update({'phase':'downloading','capture_policy':'complete-files-v1',
                            'versions_found':100,'versions_pending':58})['completion']
    assert files['completed'] == 42 and files['eta_seconds'] is None


def test_missing_inventory_is_unknown_and_metadata_regression_resets_eta():
    tracker = CaptureProgress(clock=lambda: 1)
    unknown = tracker.update({'files':64,'phase':'downloading'})['completion']
    assert unknown['total'] is None and unknown['remaining'] is None and unknown['eta_seconds'] is None
    tracker.update({'phase':'downloading','versions_found':100,'versions_pending':20})
    reset = tracker.update({'phase':'downloading','versions_found':100,'versions_pending':30})['completion']
    assert reset['completed'] == 70 and reset['eta_seconds'] is None
    invalid = tracker.update({'versions_found':-1,'versions_pending':2})['completion']
    assert invalid['total'] is None and invalid['remaining'] is None


def test_board_downloaded_receipts_and_unavailable_records_count_correctly():
    result = CaptureProgress().update({'sitepowerup':{'counts':{'captured':10,'downloaded':2,
        'pending':7,'unavailable':3,'excluded':4,'duplicate':1}}})['completion']
    assert (result['total'], result['completed'], result['remaining']) == (27, 18, 9)
