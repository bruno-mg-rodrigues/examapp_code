import 'package:flutter/material.dart';
import 'package:examapp/models.dart';

class QuestionView extends StatefulWidget {
  final Question question;
  final Function(Question) onToggleReview;

  const QuestionView({
    super.key,
    required this.question,
    required this.onToggleReview,
  });

  @override
  State<QuestionView> createState() => _QuestionViewState();
}

class _QuestionViewState extends State<QuestionView> {
  bool _showAnswer = false;
  bool _showDiscussion = false;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        automaticallyImplyLeading: false, // Remove the back arrow

        title: Text('Question ${widget.question.questionNumber}'),

        actions: [
          IconButton(
            icon: Stack(
              alignment: Alignment.center,

              children: [
                Icon(
                  widget.question.isMarkedForReview
                      ? Icons.star
                      : Icons.star_border,

                  color: Colors.black, // Black background for highlight

                  size: 28.0, // Slightly larger
                ),

                Icon(
                  widget.question.isMarkedForReview
                      ? Icons.star
                      : Icons.star_border,

                  color: widget.question.isMarkedForReview
                      ? Colors.amber
                      : Colors.white,

                  size: 24.0, // Original size
                ),
              ],
            ),

            onPressed: () => widget.onToggleReview(widget.question),
          ),
        ],
      ),

      body: SelectionArea(
        child: SingleChildScrollView(
        padding: const EdgeInsets.all(16.0),

        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,

          children: [
            Text(
              widget.question.questionText,

              style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
            ),

            const SizedBox(height: 20),

            ...widget.question.choices.map(
              (choice) => Padding(
                padding: const EdgeInsets.symmetric(vertical: 4.0),

                child: Text(
                  '${choice.letter}. ${choice.text}',

                  style: const TextStyle(fontSize: 16),
                ),
              ),
            ),

            const SizedBox(height: 20),

            ElevatedButton(
              onPressed: () {
                setState(() {
                  _showAnswer = !_showAnswer;
                });
              },

              child: Text(_showAnswer ? 'Hide Answer' : 'Show Answer'),
            ),

            if (_showAnswer) ...[
              if (widget.question.correctAnswer != null)
                Padding(
                  padding: const EdgeInsets.only(top: 10.0),
                  child: Text(
                    'ExamTopics Answer: ${widget.question.correctAnswer}',
                    style: const TextStyle(
                      fontSize: 16,
                      color: Colors.green,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                ),
              if (widget.question.voteDistribution != null &&
                  widget.question.voteDistribution!.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 5.0),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text(
                        'Community Vote Distribution:',
                        style: TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                      ...widget.question.voteDistribution!.map(
                        (vote) => Text(
                          '${vote['answer']} (${vote['percentage']}%)',
                          style: const TextStyle(
                            fontSize: 16,
                            color: Colors.blue,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
            ],
            const SizedBox(height: 30),

            if (widget.question.discussion.isNotEmpty)
              ElevatedButton(
                onPressed: () {
                  setState(() {
                    _showDiscussion = !_showDiscussion;
                  });
                },

                child: Text(
                  _showDiscussion ? 'Hide Discussion' : 'Show Discussion',
                ),
              ),

            if (_showDiscussion)
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,

                children: [
                  const SizedBox(height: 20),

                  const Text(
                    'Discussion Comments:',

                    style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
                  ),

                  const SizedBox(height: 10),

                  ...widget.question.discussion.map(
                    (comment) => Card(
                      margin: const EdgeInsets.symmetric(vertical: 8.0),

                      child: Padding(
                        padding: const EdgeInsets.all(12.0),

                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,

                          children: [
                            Text(
                              'Author: ${comment.author} - ${comment.date}',

                              style: const TextStyle(
                                fontWeight: FontWeight.bold,
                              ),
                            ),

                            const SizedBox(height: 5),

                            Text(
                              'Selected Answer: ${comment.selectedAnswer}',

                              style: const TextStyle(color: Colors.blueAccent),
                            ),

                            const SizedBox(height: 5),

                            Text(comment.comment),
                          ],
                        ),
                      ),
                    ),
                  ),
                ],
              ),
          ],
        ),
      ),
      ),
    );
  }
}
