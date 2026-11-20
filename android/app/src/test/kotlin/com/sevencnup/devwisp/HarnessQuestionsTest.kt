package com.sevencnup.devwisp

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class HarnessQuestionsTest {
    private val inbox = HarnessQuestionInbox()
    private fun request(thread: String = "deepseek:one", id: String = "q", multiple: Boolean = false, intent: String = "question") =
        HarnessQuestionRequest.fromJson(JSONObject("""{"threadId":"$thread","questionId":"$id","questions":[{"id":"pick","question":"Choose","multiSelect":$multiple,"intent":{"kind":"$intent"},"options":[{"label":"A"},{"label":"B"}]}]}"""))!!

    @Test fun questionsAndDraftsAreIsolatedBetweenThreads() {
        val a = request()
        val b = request("deepseek:two")
        inbox.receive(a); inbox.receive(b)
        inbox.answer(a.key, "pick").put("custom", "first")
        assertEquals(listOf(a), inbox.forThread("deepseek:one"))
        assertNull(inbox.response(b.key))
        assertEquals("first", inbox.response(a.key)!!.getJSONArray("answers").getJSONObject(0).getString("custom"))
    }

    @Test fun lateRecoveryCannotResurrectAResolvedQuestion() {
        val a = request()
        inbox.receive(a)
        val revision = inbox.revision
        inbox.resolve(a.key)
        inbox.reconcile(listOf(a), revision)
        assertTrue(inbox.forThread(a.key.threadId).isEmpty())
    }

    @Test fun lateEmptySnapshotCannotRemoveANewQuestion() {
        val revision = inbox.revision
        val a = request()
        inbox.receive(a)
        inbox.reconcile(emptyList(), revision)
        assertEquals(listOf(a), inbox.forThread(a.key.threadId))
    }

    @Test fun recoveryPreservesUnsubmittedDraftButClearsResolvedRequests() {
        val a = request()
        inbox.receive(a)
        inbox.answer(a.key, "pick").put("custom", "draft")
        inbox.reconcile(listOf(a), inbox.revision)
        assertNotNull(inbox.response(a.key))
        inbox.reconcile(emptyList(), inbox.revision)
        assertNull(inbox.response(a.key))
        inbox.receive(a)
        assertNull(inbox.response(a.key))
    }

    @Test fun failedSubmissionCanBeRetriedWithItsAnswers() {
        val a = request()
        inbox.receive(a)
        inbox.answer(a.key, "pick").put("selected", JSONArray(listOf("A")))
        assertTrue(inbox.beginDecision(a.key))
        assertFalse(inbox.beginDecision(a.key))
        inbox.finishDecision(a.key, false)
        assertNotNull(inbox.response(a.key))
        assertTrue(inbox.beginDecision(a.key))
        inbox.finishDecision(a.key, true)
        assertNull(inbox.response(a.key))
    }

    @Test fun singleSelectRejectsMultipleOptionsAndCustomAlongsideOptions() {
        val a = request()
        inbox.receive(a)
        val answer = inbox.answer(a.key, "pick")
        answer.put("selected", JSONArray(listOf("A", "B")))
        assertNull(inbox.response(a.key))
        answer.put("selected", JSONArray(listOf("A"))).put("custom", "other")
        assertNull(inbox.response(a.key))
        answer.remove("custom")
        assertNotNull(inbox.response(a.key))
    }

    @Test fun multiSelectAcceptsSeveralOptionsAndCustomFeedback() {
        val a = request(multiple = true)
        inbox.receive(a)
        inbox.answer(a.key, "pick").put("selected", JSONArray(listOf("A", "B"))).put("custom", "feedback")
        assertNotNull(inbox.response(a.key))
    }

    @Test fun approvalRequiresAnActualOptionAndRejectsUnknownOrDuplicateOptions() {
        val a = request(intent = "approval")
        inbox.receive(a)
        val answer = inbox.answer(a.key, "pick")
        answer.put("custom", "approve")
        assertNull(inbox.response(a.key))
        answer.remove("custom")
        answer.put("selected", JSONArray(listOf("unknown")))
        assertNull(inbox.response(a.key))
        answer.put("selected", JSONArray(listOf("A", "A")))
        assertNull(inbox.response(a.key))
        answer.put("selected", JSONArray(listOf("A")))
        assertNotNull(inbox.response(a.key))
    }

    @Test fun emptyAnswersAndOversizedFeedbackAreRejected() {
        val a = request()
        inbox.receive(a)
        assertNull(inbox.response(a.key))
        inbox.answer(a.key, "pick").put("custom", " ")
        assertNull(inbox.response(a.key))
        inbox.answer(a.key, "pick").put("custom", "a".repeat(30_001))
        assertNull(inbox.response(a.key))
    }

    @Test fun allQuestionsMustBeAnswered() {
        val json = JSONObject(request().json)
        json.getJSONArray("questions").put(JSONObject("""{"id":"other","question":"Explain"}"""))
        val a = HarnessQuestionRequest.fromJson(json)!!
        inbox.receive(a)
        inbox.answer(a.key, "pick").put("selected", JSONArray(listOf("A")))
        assertNull(inbox.response(a.key))
        inbox.answer(a.key, "other").put("custom", "explanation")
        assertEquals(2, inbox.response(a.key)!!.getJSONArray("answers").length())
    }

    @Test fun malformedRequestsAreRejected() {
        assertNull(HarnessQuestionRequest.fromJson(JSONObject()))
        assertNull(HarnessQuestionRequest.fromJson(JSONObject("""{"threadId":"t","questionId":"q","questions":[]}""")))
        assertNull(HarnessQuestionRequest.fromJson(JSONObject("""{"threadId":"t","questionId":"q","questions":[{"id":"x"},{"id":"x"}]}""")))
    }

    @Test fun connectionResetClearsQuestionsDraftsAndSubmissionState() {
        val a = request()
        inbox.receive(a)
        inbox.answer(a.key, "pick").put("custom", "draft")
        inbox.beginDecision(a.key)
        inbox.clear()
        inbox.receive(a)
        assertFalse(inbox.isSubmitting(a.key))
        assertNull(inbox.response(a.key))
    }

    @Test fun confirmedReplyIncludesAllQuestionsAndCancelHasNoUserMessage() {
        val request = HarnessQuestionRequest.fromJson(JSONObject("""{"threadId":"deepseek:one","turnId":"turn","questionId":"q","questions":[{"id":"food","question":"Food?"},{"id":"where","question":"Where?"}]}"""))!!
        val response = JSONObject("""{"answers":[{"id":"where","selected":[],"custom":" home "},{"id":"food","selected":["A","B"],"custom":"extra"}]}""")
        val reply = request.reply(response, "2026-11-18T12:00:00Z")!!
        assertEquals("1. Food?\n答：A、B、extra\n\n2. Where?\n答：home", reply.getJSONArray("content").getJSONObject(0).getString("text"))
        assertEquals("user-question-q", reply.getString("id"))
        assertNull(request.reply(JSONObject().put("cancelled", true), "2026-11-18T12:00:00Z"))
    }

    @Test fun savedReplySurvivesReloadIsPlacedBeforeContinuationAndDeduplicatesServerEcho() {
        val reply = JSONObject("""{"id":"user-question-q","type":"userMessage","turnId":"turn","createdAt":"2026-11-18T12:00:00Z","content":[{"type":"text","text":"A"}]}""")
        val saved = HarnessQuestionReplies.add(HarnessQuestionReplies.add(JSONArray(), reply), reply)
        assertEquals(1, saved.length())
        val turns = JSONArray("""[{"id":"turn","items":[{"id":"prompt","type":"userMessage","createdAt":"2026-11-18T11:59:00+00:00"},{"id":"continuation","type":"agentMessage","createdAt":"2026-11-18T12:00:01+00:00"}]}]""")
        val (merged, remaining) = HarnessQuestionReplies.merge(turns, JSONArray(saved.toString()))
        assertEquals("user-question-q", merged.getJSONObject(0).getJSONArray("items").getJSONObject(1).getString("id"))
        assertEquals(2, turns.getJSONObject(0).getJSONArray("items").length())
        val (deduplicated, cleared) = HarnessQuestionReplies.merge(merged, remaining)
        assertEquals(3, deduplicated.getJSONObject(0).getJSONArray("items").length())
        assertEquals(0, cleared.length())
    }

    @Test fun partialHistoryDoesNotMoveReplyToAnotherTurnOrForgetIt() {
        val reply = JSONObject("""{"id":"user-question-q","turnId":"original","createdAt":"2026-11-18T12:00:00Z"}""")
        val turns = JSONArray("""[{"id":"other","items":[]}]""")
        val (merged, remaining) = HarnessQuestionReplies.merge(turns, JSONArray().put(reply))
        assertEquals(0, merged.getJSONObject(0).getJSONArray("items").length())
        assertEquals(1, remaining.length())
    }

    private fun assistant(text: String, id: String = "assistant") = JSONObject()
        .put("id", id).put("type", "agentMessage").put("text", text)
    private fun reply(id: String = "q", prefix: String? = null) = JSONObject()
        .put("id", "user-question-$id").put("type", "userMessage").put("turnId", "turn")
        .put("content", JSONArray().put(JSONObject().put("type", "text").put("text", "Answer $id")))
        .apply { if (prefix != null) put("_assistantBoundary", JSONObject().put("itemId", "assistant").put("text", prefix)) }
    private fun history(vararg items: JSONObject) = JSONArray().put(JSONObject().put("id", "turn")
        .put("status", "inProgress").put("items", JSONArray(items.toList())))
    private fun contents(turns: JSONArray): List<String> {
        val items = turns.getJSONObject(0).getJSONArray("items")
        return (0 until items.length()).map { i -> items.getJSONObject(i).let {
            it.optString("text").ifEmpty { it.getJSONArray("content").getJSONObject(0).getString("text") }
        } }
    }

    @Test fun capturedBoundaryPutsConfirmedAnswerBetweenIntroAndContinuationAcrossReloads() {
        val original = history(assistant("Before\n\nAfter"))
        val saved = JSONArray().put(reply(prefix = "Before"))
        val (merged, remaining) = HarnessQuestionReplies.merge(original, saved)
        assertEquals(listOf("Before", "Answer q", "After"), contents(merged))
        assertEquals(1, remaining.length())
        assertEquals(listOf("Before\n\nAfter"), contents(original))
        val (reloaded, retained) = HarnessQuestionReplies.merge(JSONArray(original.toString()), JSONArray(remaining.toString()))
        assertEquals(contents(merged), contents(reloaded))
        assertEquals(1, retained.length())
        val (repeated, _) = HarnessQuestionReplies.merge(merged, retained)
        assertEquals(contents(merged), contents(repeated))
    }

    @Test fun earlyServerEchoIsMovedBetweenSegmentsWithoutDuplicatingIt() {
        val echo = reply().put("createdAt", "2026-11-18T12:00:00Z")
        val (merged, remaining) = HarnessQuestionReplies.merge(history(echo, assistant("Before\n\nAfter")),
            JSONArray().put(reply(prefix = "Before")))
        assertEquals(listOf("Before", "Answer q", "After"), contents(merged))
        assertEquals("2026-11-18T12:00:00Z", merged.getJSONObject(0).getJSONArray("items").getJSONObject(1).getString("createdAt"))
        assertEquals(1, remaining.length())
    }

    @Test fun multipleQuestionsUseTheirCapturedOffsetsEvenWhenRepliesAreReversed() {
        val saved = JSONArray().put(reply("two", "First\n\nSecond")).put(reply("one", "First"))
        val (merged, remaining) = HarnessQuestionReplies.merge(history(reply("one"), reply("two"),
            assistant("First\n\nSecond\n\nFinal")), saved)
        assertEquals(listOf("First", "Answer one", "Second", "Answer two", "Final"), contents(merged))
        assertEquals(2, remaining.length())
        val items = merged.getJSONObject(0).getJSONArray("items")
        assertEquals(items.length(), (0 until items.length()).map { items.getJSONObject(it).getString("id") }.toSet().size)
    }

    @Test fun mismatchedHistoryCannotBeSplitUsingAnUnrelatedPrefix() {
        val (merged, remaining) = HarnessQuestionReplies.merge(history(assistant("Replacement text")),
            JSONArray().put(reply(prefix = "Original text")))
        assertEquals(listOf("Replacement text", "Answer q"), contents(merged))
        assertEquals(1, remaining.length())
    }

    @Test fun liveSuffixKeepsOnlyItsOwnTextAndPrefixOffsetForAnimation() {
        val live = LiveReplyText("Before")
        live.append("\n\nAfter 🌙")
        live.flush()
        val source = assistant(live.visibleText).put("_liveReplyItemId", "assistant")
        val (merged, remaining) = HarnessQuestionReplies.merge(history(source), JSONArray().put(reply(prefix = "Before")))
        val items = merged.getJSONObject(0).getJSONArray("items")
        assertFalse(items.getJSONObject(0).has("_liveReplyItemId"))
        val continuation = items.getJSONObject(2)
        assertEquals("After 🌙", live.visibleText.drop(continuation.getInt("_liveReplyPrefixLength")))
        assertEquals("assistant", continuation.getString("_liveReplyItemId"))
        live.append(" Next")
        live.flush()
        val (updated, _) = HarnessQuestionReplies.merge(history(assistant(live.visibleText)), remaining)
        assertEquals(listOf("Before", "Answer q", "After 🌙 Next"), contents(updated))
    }

    @Test fun answerAtCurrentEndKeepsBoundaryUntilTheTurnActuallyCompletes() {
        val saved = JSONArray().put(reply(prefix = "Before"))
        val original = history(assistant("Before"), reply())
        val (_, remaining) = HarnessQuestionReplies.merge(original, saved)
        assertEquals(1, remaining.length())
        val completed = history(assistant("Before"), reply(), assistant("After", "continuation"))
        completed.getJSONObject(0).put("status", "completed")
        val (merged, cleared) = HarnessQuestionReplies.merge(completed, remaining)
        assertEquals(listOf("Before", "Answer q", "After"), contents(merged))
        assertEquals(0, cleared.length())
    }

    @Test fun anchoredReplyPrecedesLiveContinuationWithoutRelyingOnTimestamps() {
        val request = HarnessQuestionRequest.fromJson(JSONObject(request().json)
            .put("turnId", "turn").put("afterItemId", "intro").put("timelineVersion", 1))!!
        val reply = request.reply(JSONObject("""{"answers":[{"id":"pick","selected":["A"]}]}"""), "2026-11-18T12:00:00Z")!!
        assertEquals("intro", reply.getString("afterItemId"))
        val original = history(assistant("Before", "intro"), assistant("After", "continuation"))
        val (merged, remaining) = HarnessQuestionReplies.merge(original, JSONArray().put(reply))
        assertEquals(listOf("Before", "1. Choose\n答：A", "After"), contents(merged))
        val (echoed, cleared) = HarnessQuestionReplies.merge(merged, remaining)
        assertEquals(contents(merged), contents(echoed))
        assertEquals(0, cleared.length())
    }

    @Test fun questionWithoutIntroPutsItsAnswerBeforeTheFirstLiveAssistantText() {
        val (merged, _) = HarnessQuestionReplies.merge(history(assistant("After")),
            JSONArray().put(reply().put("afterItemId", "")))
        assertEquals(listOf("Answer q", "After"), contents(merged))
    }

    @Test fun partialHistoryKeepsCapturedBoundaryForItsOriginalTurn() {
        val saved = JSONArray().put(reply(prefix = "Before"))
        val (empty, remaining) = HarnessQuestionReplies.merge(JSONArray(), saved)
        assertEquals(0, empty.length())
        val (restored, _) = HarnessQuestionReplies.merge(history(assistant("Before\n\nAfter")), remaining)
        assertEquals(listOf("Before", "Answer q", "After"), contents(restored))
    }
}
